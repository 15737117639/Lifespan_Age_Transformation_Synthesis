### Copyright (C) 2020 Roy Or-El. All rights reserved.
### Licensed under the CC BY-NC-SA 4.0 license (https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode).
import numpy as np
import torch
import torch.nn as nn
import re
import functools
from collections import OrderedDict
from .base_model import BaseModel
import util.util as util
from . import networks
from pdb import set_trace as st

class LATS(BaseModel): #Lifetime Age Transformation Synthesis
    def name(self):
        return 'LATS'

    def init_loss_filter(self):
        def loss_filter(g_gan, g_cycle, g_rec, d_real, d_fake, grad_penalty, identity_reconst, age_reconst):
            return [l for l in (g_gan, g_cycle, g_rec, d_real, d_fake, grad_penalty, identity_reconst, age_reconst)]
        return loss_filter

    def initialize(self, opt):
        BaseModel.initialize(self, opt)

        # if opt.resize_or_crop != 'none': # when training at full res this causes OOM
        torch.backends.cudnn.benchmark = True

        # determine mode of operation [train, test, deploy, traverse (latent interpolation)]
        self.isTrain = opt.isTrain
        self.traverse = (not self.isTrain) and opt.traverse

        # mode to generate Fig. 15 in the paper
        self.compare_to_trained_outputs = (not self.isTrain) and opt.compare_to_trained_outputs
        if self.compare_to_trained_outputs:
            self.compare_to_trained_class = opt.compare_to_trained_class
            self.trained_class_jump = opt.trained_class_jump

        self.deploy = (not self.isTrain) and opt.deploy
        if not self.isTrain and opt.random_seed != -1:
            torch.manual_seed(opt.random_seed)
            torch.cuda.manual_seed_all(opt.random_seed)
            np.random.seed(opt.random_seed)

        # network architecture parameters
        self.nb = opt.batchSize
        self.size = opt.fineSize
        self.ngf = opt.ngf
        self.ngf_global = self.ngf

        self.numClasses = opt.numClasses
        self.use_orig_age_features_within_domain = opt.use_orig_age_features_within_domain
        self.use_moving_avg = not opt.no_moving_avg

        self.no_cond_noise = opt.no_cond_noise
        style_dim = opt.gen_dim_per_style * self.numClasses
        self.duplicate = opt.gen_dim_per_style

        self.cond_length = style_dim

        # self.active_classes_mapping = opt.active_classes_mapping

        if not self.isTrain:
            self.fgnet = opt.fgnet
            self.debug_mode = opt.debug_mode
        else:
            self.fgnet = False
            self.debug_mode = False

        ##### define networks
        # Generators
        self.netG = self.parallelize(networks.define_G(opt.input_nc, opt.output_nc, opt.ngf, opt.n_downsample,
                                     id_enc_norm=opt.id_enc_norm, gpu_ids=self.gpu_ids, padding_type='reflect', style_dim=style_dim,
                                     init_type='kaiming', conv_weight_norm=opt.conv_weight_norm,
                                     decoder_norm=opt.decoder_norm, activation=opt.activation,
                                     adaptive_blocks=opt.n_adaptive_blocks, normalize_mlp=opt.normalize_mlp,
                                     modulated_conv=opt.use_modulated_conv))
        if self.isTrain and self.use_moving_avg:
            self.g_running = networks.define_G(opt.input_nc, opt.output_nc, opt.ngf, opt.n_downsample,
                                               id_enc_norm=opt.id_enc_norm, gpu_ids=self.gpu_ids, padding_type='reflect', style_dim=style_dim,
                                               init_type='kaiming', conv_weight_norm=opt.conv_weight_norm,
                                               decoder_norm=opt.decoder_norm, activation=opt.activation,
                                               adaptive_blocks=opt.n_adaptive_blocks, normalize_mlp=opt.normalize_mlp,
                                               modulated_conv=opt.use_modulated_conv)
            self.g_running.train(False)
            self.requires_grad(self.g_running, flag=False)
            self.accumulate(self.g_running, self.netG, decay=0)

        # Discriminator network
        if self.isTrain:
            self.netD = self.parallelize(networks.define_D(opt.output_nc, opt.ndf, n_layers=opt.n_layers_D,
                                         numClasses=self.numClasses, gpu_ids=self.gpu_ids,
                                         init_type='kaiming'))

        if self.opt.verbose:
                print('---------- Networks initialized -------------')

        # load networks
        if not self.isTrain or opt.continue_train or opt.load_pretrain:
            pretrained_path = '' if (not self.isTrain) or (self.isTrain and opt.continue_train) else opt.load_pretrain
            if self.isTrain:
                self.load_network(self.netG, 'G_tex', opt.which_epoch, pretrained_path)
                self.load_network(self.netD, 'D_tex', opt.which_epoch, pretrained_path)
                if self.use_moving_avg:
                    self.load_network(self.g_running, 'g_running', opt.which_epoch, pretrained_path)
            elif self.use_moving_avg:
                self.load_network(self.netG, 'g_running', opt.which_epoch, pretrained_path)
            else:
                self.load_network(self.netG, 'G', opt.which_epoch, pretrained_path)


        # set loss functions and optimizers
        if self.isTrain:
            # define loss functions
            self.criterionGAN = self.parallelize(networks.SelectiveClassesNonSatGANLoss())
            self.R1_reg = networks.R1_reg()
            self.age_reconst_criterion = self.parallelize(networks.FeatureConsistency())
            self.identity_reconst_criterion = self.parallelize(networks.FeatureConsistency())
            self.criterionCycle = self.parallelize(networks.FeatureConsistency()) #torch.nn.L1Loss()
            self.criterionRec = self.parallelize(networks.FeatureConsistency()) #torch.nn.L1Loss()

            # Names so we can breakout loss
            self.loss_filter = self.init_loss_filter()
            self.loss_names = self.loss_filter('G_Adv','G_Cycle','G_Rec','D_real', 'D_fake', 'Grad_penalty',
                                               'Identity_reconst', 'Age_reconst')

            # initialize optimizers
            self.old_lr = opt.lr

            # set optimizer G
            paramsG = []
            params_dict_G = dict(self.netG.named_parameters())
            # set the MLP learning rate to 0.01 or the global learning rate
            for key, value in params_dict_G.items():
                decay_cond = ('decoder.mlp' in key)
                if opt.decay_adain_affine_layers:
                    decay_cond = decay_cond or ('class_std' in key) or ('class_mean' in key)
                if decay_cond:
                    paramsG += [{'params':[value],'lr':opt.lr * 0.01,'mult':0.01}]
                else:
                    paramsG += [{'params':[value],'lr':opt.lr}]

            self.optimizer_G = torch.optim.Adam(paramsG, lr=opt.lr, betas=(opt.beta1, opt.beta2))

            # set optimizer D
            paramsD = list(self.netD.parameters())
            self.optimizer_D = torch.optim.Adam(paramsD, lr=opt.lr, betas=(opt.beta1, opt.beta2))


    def parallelize(self, model):
        # parallelize a network
        if self.isTrain and len(self.gpu_ids) > 0:
            return networks._CustomDataParallel(model)
        else:
            return model


    def requires_grad(self, model, flag=True):
        # freeze network weights
        for p in model.parameters():
            p.requires_grad = flag


    def accumulate(self, model1, model2, decay=0.999):
        # implements exponential moving average
        params1 = dict(model1.named_parameters())
        params2 = dict(model2.named_parameters())
        model1_parallel = isinstance(model1, nn.DataParallel)
        model2_parallel = isinstance(model2, nn.DataParallel)

        for k in params1.keys():
            if model2_parallel and not model1_parallel:
                k2 = 'module.' + k
            elif model1_parallel and not model2_parallel:
                k2 = re.sub('module.', '', k)
            else:
                k2 = k
            params1[k].data.mul_(decay).add_(1 - decay, params2[k2].data)


    def encode_input(self, data, mode='train'):
        # set input data to feed to the network
        if mode == 'train':
            real_A = data['A']
            real_B = data['B']

            self.class_A = data['A_class']
            self.class_B = data['B_class']

            self.reals = torch.cat((real_A, real_B), 0)

            if len(self.gpu_ids) > 0:
                self.reals = self.reals.cuda()

        else:
            inputs = data['Imgs']
            if inputs.dim() > 4:
                inputs = inputs.squeeze(0)

            self.class_A = data['Classes']
            if self.class_A.dim() > 1:
                self.class_A = self.class_A.squeeze(0)

            if torch.is_tensor(data['Valid']):
                self.valid = data['Valid'].bool()
            else:
                self.valid = torch.ones(1, dtype=torch.bool)

            if self.valid.dim() > 1:
                self.valid = self.valid.squeeze(0)

            if isinstance(data['Paths'][0], tuple):
                self.image_paths = [path[0] for path in data['Paths']]
            else:
                self.image_paths = data['Paths']

            self.isEmpty = False if any(self.valid) else True
            if not self.isEmpty:
                available_idx = torch.arange(len(self.class_A))
                select_idx = torch.masked_select(available_idx, self.valid).long()
                inputs = torch.index_select(inputs, 0, select_idx)

                self.class_A = torch.index_select(self.class_A, 0, select_idx)
                self.image_paths = [val for i, val in enumerate(self.image_paths) if self.valid[i] == 1]

            self.reals = inputs

            if len(self.gpu_ids) > 0:
                self.reals = real_A.cuda()


    def get_conditions(self, mode='train'):
        # set conditional inputs to the network
        if mode == 'train':
            nb = self.masked_reals.shape[0] // 2
        elif self.traverse or self.deploy:
            if self.traverse and self.compare_to_trained_outputs:
                nb = 2
            else:
                nb = self.numClasses
        else:
            nb = self.numValid

        is_adain_gen = True

        #tex condition mapping
        condG_A_global = self.Tensor(nb, self.cond_length)
        condG_B_global = self.Tensor(nb, self.cond_length)
        condG_A_orig = self.Tensor(nb, self.cond_length)
        condG_B_orig = self.Tensor(nb, self.cond_length)

        if self.no_cond_noise:
            noise_sigma = 0
        else:
            noise_sigma = 0.2

        for i in range(nb):
            condG_A_global[i, :] = (noise_sigma * torch.randn(1, self.cond_length)).cuda()
            condG_A_global[i, self.class_B[i]*self.duplicate:(self.class_B[i] + 1)*self.duplicate] += 1
            if not (self.traverse or self.deploy):
                condG_B_global[i, :] = (noise_sigma * torch.randn(1, self.cond_length)).cuda()
                condG_B_global[i, self.class_A[i]*self.duplicate:(self.class_A[i] + 1)*self.duplicate] += 1

                condG_A_orig[i, :] = (noise_sigma * torch.randn(1, self.cond_length)).cuda()
                condG_A_orig[i, self.class_A[i]*self.duplicate:(self.class_A[i] + 1)*self.duplicate] += 1

                condG_B_orig[i, :] = (noise_sigma * torch.randn(1, self.cond_length)).cuda()
                condG_B_orig[i, self.class_B[i]*self.duplicate:(self.class_B[i] + 1)*self.duplicate] += 1

        if mode == 'train':
            if is_adain_gen:
                self.gen_conditions =  torch.cat((condG_A_global, condG_B_global), 0) #torch.cat((self.class_B, self.class_A), 0)
                self.rec_conditions = torch.cat((condG_B_global, condG_A_global), 0)
                self.orig_conditions = torch.cat((condG_A_orig, condG_B_orig),0)
        else:
            self.gen_conditions = condG_A_global #self.class_B
            if not (self.traverse or self.deploy):
                self.rec_conditions = condG_B_global #self.class_A
                self.orig_conditions = condG_A_orig


    def update_G(self, infer=False):
        # Generator optimization setp
        self.optimizer_G.zero_grad()

        self.get_conditions()
        gen_in = self.reals

        gen_embeddings = self.gen_conditions
        rec_embeddings = self.rec_conditions
        orig_embeddings = self.orig_conditions

        ############### multi GPU ###############
        reconst_tex_images, generated_tex_images, cyc_tex_images, orig_id_features,
        orig_age_features, fake_id_features, fake_age_features = \
        self.netG(gen_in, rec_embeddings, gen_embeddings, orig_embeddings)

        #discriminator pass
        disc_in = generated_tex_images
        disc_out = self.netD(disc_in)

        #self-reconstruction loss
        if self.opt.lambda_rec > 0:
            loss_G_Rec = self.criterionRec(reconst_tex_images, gen_in) * self.opt.lambda_rec
        else:
            loss_G_Rec = torch.zeros(1).cuda()

        #cycle loss
        if self.opt.lambda_cyc > 0:
            loss_G_Cycle = self.criterionCycle(cyc_tex_images, gen_in) * self.opt.lambda_cyc
        else:
            loss_G_Cycle = torch.zeros(1).cuda()

        # identity feature loss
        loss_G_identity_reconst = self.identity_reconst_criterion(fake_id_features, orig_id_features) * self.opt.lambda_id
        # age feature loss
        loss_G_age_reconst = self.age_reconst_criterion(fake_age_features, gen_embeddings) * self.opt.lambda_age
        # orig age feature loss
        loss_G_age_reconst += self.age_reconst_criterion(orig_age_features, orig_embeddings) * self.opt.lambda_age

        # adversarial loss
        target_classes = torch.cat((self.class_B,self.class_A),0)
        loss_G_GAN = self.criterionGAN(disc_out, target_classes, True, is_gen=True)

        # overall loss
        loss_G = (loss_G_GAN + loss_G_Rec + loss_G_Cycle + \
        loss_G_identity_reconst + loss_G_age_reconst).mean()

        loss_G.backward()
        self.optimizer_G.step()

        # update exponential moving average
        if self.use_moving_avg:
            self.accumulate(self.g_running, self.netG)

        # generate images for visdom
        if infer:
            if self.use_moving_avg:
                with torch.no_grad():
                    orig_id_features_out, _ = self.g_running.encode(gen_in)
                    #within domain decode
                    if self.opt.lambda_rec > 0:
                        if self.use_orig_age_features_within_domain:
                            reconst_tex_images_out = self.g_running(gen_in, None)
                        else:
                            reconst_tex_images_out, _, _, _ = self.g_running.decode(orig_id_features_out, None, rec_embeddings)

                    #cross domain decode
                    generated_tex_images_out, _, _, _ = self.g_running.decode(orig_id_features_out, None, gen_embeddings)
                    #encode generated
                    fake_id_features_out, _ = self.g_running.encode(generated_tex_images, self.masks)
                    #decode generated
                    if self.opt.lambda_cyc > 0:
                        cyc_tex_images_out, _, _, _ = self.g_running.decode(fake_id_features_out, None, rec_embeddings)
            else:
                generated_tex_images_out = generated_tex_images
                if self.opt.lambda_rec > 0:
                    reconst_tex_images_out = reconst_tex_images
                if self.opt.lambda_cyc > 0:
                    cyc_tex_images_out = cyc_tex_images

        loss_dict = {'loss_G_GAN': loss_G_GAN.mean(), 'loss_G_Cycle': loss_G_Cycle.mean(),
                     'loss_G_Rec': loss_G_Rec.mean(), 'loss_G_identity_reconst': loss_G_identity_reconst.mean(),
                     'loss_G_age_reconst': loss_G_age_reconst.mean()}

        return [loss_dict,
                None if not infer else gen_in,
                None if not infer else generated_tex_images_out,
                None if not infer else reconst_tex_images_out,
                None if not infer else cyc_tex_images_out]


    def update_D(self):
        # Discriminator optimization setp
        self.optimizer_D.zero_grad()
        self.get_conditions()
        gen_in = self.reals

        ############### multi GPU ###############
        _, generated_tex_images, _, _, _, _, _ = self.netG(gen_in, None, self.gen_conditions, None, disc_pass=True)

        #fake discriminator pass
        fake_disc_in = generated_tex_images.detach()
        fake_disc_out = self.netD(fake_disc_in)

        #real discriminator pass
        real_disc_in = gen_in

        # necessary for R1 regularization
        real_disc_in.requires_grad_()

        real_disc_out = self.netD(real_disc_in)

        #Fake GAN loss
        fake_target_classes = torch.cat((self.class_B,self.class_A),0)
        loss_D_fake = self.criterionGAN(fake_disc_out, fake_target_classes, False, is_gen=False)

        #Real GAN loss
        real_target_classes = torch.cat((self.class_A,self.class_B),0)
        loss_D_real = self.criterionGAN(real_disc_out, real_target_classes, True, is_gen=False)

        # R1 regularization
        loss_D_reg = self.R1_reg(real_disc_out, real_disc_in)

        loss_D = (loss_D_fake + loss_D_real + loss_D_reg).mean()
        loss_D.backward()
        self.optimizer_D.step()

        return {'loss_D_real': loss_D_real.mean(), 'loss_D_fake': loss_D_fake.mean(), 'loss_D_reg': loss_D_reg.mean()}


    def inference(self, data):
        # Encode Inputs
        self.encode_input(data, mode='test')
        if self.isEmpty:
            return

        self.numValid = self.valid.sum().item()
        sz = self.reals.size()
        self.fake_B_tex = self.Tensor(self.numClasses, sz[0], sz[1], sz[2], sz[3])
        self.rec_A_tex = self.Tensor(self.numClasses, sz[0], sz[1], sz[2], sz[3])

        with torch.no_grad():
            if self.traverse or self.deploy:
                if self.traverse and self.compare_to_trained_outputs:
                    start = self.compare_to_trained_class - self.trained_class_jump
                    end = start + (self.trained_class_jump * 2) * 2 #arange is between [start, end), end is always omitted
                    self.class_B = torch.arange(start, end, step=self.trained_class_jump*2, dtype=self.class_A.dtype)
                else:
                    self.class_B = torch.arange(self.numClasses, dtype=self.class_A.dtype)

                self.get_conditions(mode='test')

                self.tex_in = self.reals
                within_domain_idx = -1
                self.fake_B_tex = self.netG.infer(self.tex_in, None, self.gen_conditions, within_domain_idx, traverse=self.traverse, deploy=self.deploy, interp_step=self.opt.interp_step)
            else:
                for i in range(self.numClasses):
                    self.class_B = self.Tensor(self.numValid).long().fill_(i)
                    self.get_conditions(mode='test')
                    self.tex_in = self.reals

                    if self.use_orig_age_features_within_domain:
                        within_domain_idx = i
                    else:
                        within_domain_idx = -1

                    if self.isTrain:
                        self.fake_B_tex[i, :, :, :, :] = self.g_running.infer(tex_in, None, self.gen_conditions, within_domain_idx)
                    else:
                        self.fake_B_tex[i, :, :, :, :] = self.netG.infer(tex_in, None, self.gen_conditions, within_domain_idx)

                    fake_B_tex_rec_input = self.fake_B_tex[i, :, :, :, :]

                    if self.isTrain:
                        self.rec_A_tex[i, :, :, :, :] = self.g_running.infer(fake_B_tex_rec_input, None, self.rec_conditions, within_domain_idx)
                    else:
                        self.rec_A_tex[i, :, :, :, :] = self.netG.infer(fake_B_tex_rec_input, None, self.rec_conditions, within_domain_idx)

            visuals = self.get_visuals()

        return visuals


    def save(self, which_epoch):
        self.save_network(self.netG, 'G', which_epoch, self.gpu_ids)
        self.save_network(self.netD, 'D', which_epoch, self.gpu_ids)
        if self.use_moving_avg:
            self.save_network(self.g_running, 'g_running', which_epoch, self.gpu_ids)


    def update_learning_rate(self):
        lr = self.old_lr * self.opt.decay_gamma
        for param_group in self.optimizer_D.param_groups:
            param_group['lr'] = lr
        for param_group in self.optimizer_G.param_groups:
            mult = param_group.get('mult', 1.0)
            param_group['lr'] = lr * mult
        if self.opt.verbose:
            print('update learning rate: %f -> %f' % (self.old_lr, lr))
        self.old_lr = lr


    def get_visuals(self):
        return_dicts = [OrderedDict() for i in range(self.numValid)]

        real_A = util.tensor2im(self.tex_in.data)
        fake_B_tex = util.tensor2im(self.fake_B_tex.data)

        if self.debug_mode:
            rec_A_tex = util.tensor2im(self.rec_A_tex.data[:,:,:,:,:])

        if self.numValid == 1:
            real_A = np.expand_dims(real_A, axis=0)

        for i in range(self.numValid):
            # get the original image and the results for the current samples
            curr_real_A = real_A[i, :, :, :]
            real_A_img = curr_real_A[:, :, :3]

            # set output classes numebr
            if self.traverse:
                out_classes = curr_fake_B_tex.shape[0]
            else:
                out_classes = self.numClasses

            # start with age progression/regression images
            if self.traverse or self.deploy:
                curr_fake_B_tex = fake_B_tex
                orig_dict = OrderedDict([('orig_img', real_A_img)])
            else:
                curr_fake_B_tex = fake_B_tex[:, i, :, :, :]
                orig_dict = OrderedDict([('orig_img_cls_' + str(self.class_A[i].item()), real_A_img)])

            return_dicts[i].update(orig_dict)

            for j in range(out_classes):
                fake_res_tex = curr_fake_B_tex[j, :, :, :3]
                fake_dict_tex = OrderedDict([('tex_trans_to_class_' + str(j), fake_res_tex)])
                return_dicts[i].update(fake_dict_tex)

            if not (self.traverse or self.deploy):
                if self.debug_mode and (not self.fgnet):
                    # continue with tex reconstructions
                    curr_rec_A_tex = rec_A_tex[:, i, :, :, :]
                    orig_dict = OrderedDict([('orig_img2', real_A_img)])
                    return_dicts[i].update(orig_dict)
                    for j in range(self.numClasses):
                        rec_res_tex = curr_rec_A_tex[j, :, :, :3]
                        rec_dict_tex = OrderedDict([('tex_rec_from_class_' + str(j), rec_res_tex)])
                        return_dicts[i].update(rec_dict_tex)

        return return_dicts


class InferenceModel(LATS):
    def forward(self, data):
        return self.inference(data)
