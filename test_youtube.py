import os
from collections import OrderedDict
from options.test_options import TestOptions
from data.data_loader import CreateDataLoader
from models.models import create_model
import util.util as util
from util.visualizer import Visualizer
from util import html
import torch
from pdb import set_trace as st


def get_curr_id(paths):
    basename = os.path.basename(paths[0])
    underscore_token = basename.rfind('_')

    curr_id = basename[:underscore_token]

    # last_slash = paths[0].rfind('/')
    # underscore_token = paths[0].rfind('_')
    #
    # curr_id = paths[0][last_slash+1:underscore_token]

    return 'Ground Truth for ID ' + curr_id

def get_gt_visuals(real_imgs, paths):
    gt_dict = OrderedDict()
    for i in range(len(paths)):
        curr_gt = util.tensor2im(real_imgs[i, :3, :, :])

        underscore_token = paths[i].rfind('_')
        dot_token = paths[i].rfind('.')
        curr_age = paths[i][underscore_token+1:dot_token]

        curr_gt_dict = OrderedDict([('Age_' + curr_age, curr_gt)])
        gt_dict.update(curr_gt_dict)

    return gt_dict


def test_youtube(opt):
    opt.nThreads = 1   # test code only supports nThreads = 1
    opt.batchSize = 1  # test code only supports batchSize = 1
    opt.serial_batches = True  # no shuffle
    opt.no_flip = True  # no flip

    data_loader = CreateDataLoader(opt)
    dataset = data_loader.load_data()
    dataset_size = len(data_loader)

    print('#test subjects = %d' % dataset_size)
    visualizer = Visualizer(opt)
    model = create_model(opt)

    # create website
    ex = os.path.basename(opt.dataroot)
    web_dir = './evaluation/Youtube/results/youtube_{}_eval_{}'.format(ex, opt.name)
    counter = 1

    if not os.path.isdir(web_dir):
        os.makedirs(web_dir)
    else:
        while os.path.isdir(web_dir + '_' + str(counter)):
            counter += 1
        os.makedirs(web_dir + '_' + str(counter))

    web_dir = web_dir + '_' + str(counter)
    webpage = html.HTML(web_dir, 'Youtube Evaluation - {}s'.format(ex))

    # evaluate
    for i, data in enumerate(dataset):
        if i >= opt.how_many:
            break

        visuals = model.inference(data)
        img_path = [path[0] for path in data['Paths']]
        gt_path = get_curr_id(img_path)
        for i, path in enumerate(img_path):
            print('process image... %s' % path)

        gt_visuals = get_gt_visuals(data['Imgs'].squeeze(0), img_path)
        visualizer.save_images(webpage, visuals, img_path, gt_visuals=gt_visuals, gt_path=gt_path)

        webpage.save()

if __name__ == "__main__":
    opt = TestOptions().parse(save=False)
    test_youtube(opt)
