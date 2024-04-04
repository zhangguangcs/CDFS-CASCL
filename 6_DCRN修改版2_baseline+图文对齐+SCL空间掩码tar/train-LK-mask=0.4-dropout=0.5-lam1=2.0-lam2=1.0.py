import numpy as np
import os
import argparse
import pickle
import time
import imp
import logging
from sklearn import metrics
from sklearn.neighbors import KNeighborsClassifier

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from torch.utils.tensorboard import SummaryWriter
# from sentence_transformers import SentenceTransformer

from model.mapping import Mapping
from model.encoder import Encoder
from model.SimSiam_block import SimSiam
from model.classifier import C_F_Classifier
from utils.dataloader import get_HBKC_data_loader, Task, get_target_dataset, getMetaTrainLabeledDataset, get_metatrain_Labeled_data_loader, tagetSSLDataset
from utils import utils, encode_class_label, loss_function, data_augment

# from practice import t_sne
from sklearn.manifold import TSNE

parser = argparse.ArgumentParser(description="Few Shot Visual Recognition")
parser.add_argument('--config', type=str, default=os.path.join( './config', 'longkou.py'))
args = parser.parse_args()

# 加载超参数
config = imp.load_source("", args.config).config # (name, pathname)
train_opt = config['train_config'] # 得到train_opt对象

data_path = config['data_path']
save_path = config['save_path']
source_data = config['source_data']
target_data = config['target_data']
target_data_gt = config['target_data_gt']
log_dir = config['log_dir']
patch_size = train_opt['patch_size']
batch_task = train_opt['batch_task']
emb_size = train_opt['d_emb']
SRC_INPUT_DIMENSION = train_opt['src_input_dim']
TAR_INPUT_DIMENSION = train_opt['tar_input_dim']
N_DIMENSION = train_opt['n_dim']
SRC_CLASS_NUM = train_opt['src_class_num']
SHOT_NUM_PER_CLASS = train_opt['shot_num_per_class']
QUERY_NUM_PER_CLASS = train_opt['query_num_per_class']
EPISODE = train_opt['episode']
COARSE_EP = train_opt['coarse_ep']
FINE_EP = train_opt['fine_ep']
LEARNING_RATE = train_opt['lr']
lambda_1 = train_opt['lambda_1']
GPU = config['gpu']
TAR_CLASS_NUM = train_opt['tar_class_num'] # the number of class
TAR_LSAMPLE_NUM_PER_CLASS = train_opt['tar_lsample_num_per_class'] # the number of labeled samples per class
# hid_units = train_opt['hid_units']
HIDDEN_CHANNELS = train_opt['hidden_channels']
WEIGHT_DECAY = train_opt['weight_decay']

utils.same_seeds(0)



# get src/tar class number -> label semantic vector
labels_src = ["water", "bare soil school", "bare soil park", "bare soil farmland", "natural plants", "weeds in farmland", "forest", "grass", "rice field grown", "rice field first stage", "row crops", "plastic house", "manmade non dark", "manmade dark", "manmade blue", "manmade red", "manmade grass", "asphalt"]
# UP
# labels_tar = ["Asphalt", "Meadows", "Gravel", "Trees", "Painted metal sheets", "Bare Soil", "Bitumen", "Self Blocking Bricks", "Shadows"]
# IP
# labels_tar = ["Alfalfa", "Corn notill", "Corn mintill", "Corn", "Grass pasture", "Grass trees", "Grass pasture mowed", "Hay windrowed", "Oats", "Soybean notill", "Soybean mintill", "Soybean clean", "Wheat", "Woods", "Buildings Grass Trees Drives", "Stone Steel Towers"]
# salinas
# labels_tar = ["Brocoli green weeds 1", "Brocoli green weeds 2", "Fallow", "Fallow rough plow", "Fallow smooth", "Stubble", "Celery", "Grapes untrained", "Soil vinyard develop", "Corn senesced green weeds","Lettuce romaine 4wk", "Lettuce romaine 5wk", "Lettuce romaine 6wk", "Lettuce romaine 7wk" , "Vinyard untrained", "Vinyard vertical trellis"]

# HC
# labels_tar = ["Strawberry", "Cowpea", "Soybean", "Sorghum", "Water spinach", "Watermelon", "Greens", "Trees", "Grass", "Red roof","Gray roof", "Plastic", "Bare soil", "Road" , "Bright object", "Water"]

# LK
labels_tar = ["Corn", "Cotton", "Sesame", "Broad-leaf soybean", "Narrow-leaf soybean", "Rice", "Water", "Roads and houses", "Mixed weed"]

# houston
# labels_tar = ["Healthy grass", "Stressed grass", "Synthetic grass", "Trees", "Soil", "Water", "Residential", "Commercial", "Road", "Highway", "Railway", "Parking Lot 1", "Parking Lot 2", "Tennis Court", "Running Track"]


# load pretrained Bert
# model = SentenceTransformer('bert-base-nli-mean-tokens')
# model.eval()

# new pretrained Bert
from transformers import BertModel, BertTokenizer
model = BertModel.from_pretrained('../bert-base-uncased') # base 768 large 768
model.eval()
tokenizer = BertTokenizer.from_pretrained('../bert-base-uncased')

encoded_inputs_src = tokenizer(labels_src, padding=True, truncation=True, return_tensors='pt')
with torch.no_grad():
    outputs_src = model(**encoded_inputs_src)
semantic_mapping_src = outputs_src.last_hidden_state[:, 0, :]  # (num_classess, 768)

encoded_inputs_tar = tokenizer(labels_tar, padding=True, truncation=True, return_tensors='pt')
with torch.no_grad():
    outputs_tar = model(**encoded_inputs_tar)
semantic_mapping_tar = outputs_tar.last_hidden_state[:, 0, :]  # (num_classess, 768)


semantic_mapping_src = semantic_mapping_src.cpu().numpy()
semantic_mapping_tar = semantic_mapping_tar.cpu().numpy()


# 加载源域数据
with open(os.path.join(data_path, source_data), 'rb') as handle: # rb指的是二进制只读
    source_imdb = pickle.load(handle) # pickle.load() 将类文件对象转化为对象，可以看作文件转化为对象，反序列化过程。

data_train = source_imdb['data'] # (77592, 9, 9, 128)
labels_train = source_imdb['Labels'] # (77592, )

# 获取源域CH全部19个类的数据字典train_dict
keys_all_train = sorted(list(set(labels_train)))  # [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18]
label_encoder_train = {}
for i in range(len(keys_all_train)):
    label_encoder_train[keys_all_train[i]] = i  # {0:0, 1:1,...,...}
train_dict = {}
for class_, path in zip(labels_train, data_train):
    if label_encoder_train[class_] not in train_dict:
        train_dict[label_encoder_train[class_]] = []
    train_dict[label_encoder_train[class_]].append(path) # 按类别划分源域数据 {5:[[data5],[data6]],1:[data0],[data1]}
del keys_all_train
del label_encoder_train

# 获取源域CH用于元训练的18个类的数据字典。CH共19个类，将以前的数据集少于200个样本的类别过滤掉，剩下18个类别，每类200个标记样本
metatrain_data = utils.sanity_check(train_dict) # dict:18 list:200 (9,9,128)

# 从源域元训练数据获取精细类到粗糙类的知识结构
# num_fine = len(metatrain_data) # 18
# num_coarse = 12 # 4, 6, 8, 10, 12
#
# src_metatrain_data, src_metatrain_label = getMetaTrainLabeledDataset(metatrain_data) # (3600,128,9,9) (3600,)
# relation = utils.gen_superclass(src_metatrain_data, src_metatrain_label, num_fine, num_coarse) # 返回的是精细类对应的粗糙类关系 取值是粗类标签的范围 [ 7 11 10 10  9  9  1  9  1  7  6  8  3  5  2  0 11  4]

for class_ in metatrain_data: # 200 * 18 = 3600
    for i in range(len(metatrain_data[class_])):
        metatrain_data[class_][i] = np.transpose(metatrain_data[class_][i], (2, 0, 1)) # (9,9,128) -> (128,9,9)

# 加载目标域数据
test_data = os.path.join(data_path,target_data)
test_label = os.path.join(data_path,target_data_gt)
Data_Band_Scaler, GroundTruth = utils.load_data(test_data, test_label)

# 损失初始化
crossEntropy = nn.CrossEntropyLoss().to(GPU)
cos_criterion = nn.CosineSimilarity(dim=1).to(GPU)

infoNCE_Loss = loss_function.ContrastiveLoss(batch_size = TAR_CLASS_NUM).to(GPU)
infoNCE_Loss_SSL = loss_function.ContrastiveLoss(batch_size = 128).to(GPU)
SupConLoss_t = loss_function.SupConLoss(temperature=0.1).to(GPU)

# 实验结果指标
# nDataSet = 10
nDataSet = 10
acc = np.zeros([nDataSet, 1]) # 每轮的准确率
A = np.zeros([nDataSet, TAR_CLASS_NUM]) # 每轮每类的准确率
k = np.zeros([nDataSet, 1]) # Kappa
best_predict_all = [] # 最好的预测结果，存什么
best_G, best_RandPerm, best_Row, best_Column, best_nTrain = None,None,None,None,None

seeds = [1336, 1227, 1228, 1233, 1231, 1236, 1226, 1235, 1337, 1224] # LK, same as HC
# seeds = [ 1226, 1235, 1337, 1224] # LK, same as HC

# 日志设置
experimentSetting = '{}way_{}shot_{}'.format(TAR_CLASS_NUM, TAR_LSAMPLE_NUM_PER_CLASS, target_data.split('/')[0])
utils.set_logging_config(os.path.join(log_dir, experimentSetting), nDataSet)  # ./logs/9way_5shot_UP/
# 日志初始化
logger = logging.getLogger('main')
logger.info('seeds_list:{}'.format(seeds))

for iDataSet in range(nDataSet) :
    logger.info('emb_size:{}'.format(emb_size))
    logger.info('num_generation:{}'.format(config['num_generation']))
    logger.info('patch_size:{}'.format(patch_size))
    logger.info('seeds:{}'.format(seeds[iDataSet]))

    # np.random.seed(seeds[iDataSet]) # 【严重错误】
    utils.same_seeds(seeds[iDataSet])  # 正确：每次都固定所有

    # 源域每类200个数据普通分类的数据加载器 (3600,128,9,9) (3600,)
    # source_data_loader = get_metatrain_Labeled_data_loader(src_metatrain_data, src_metatrain_label)

    #  load target domain data for training and testing
    train_loader, test_loader, target_da_metatrain_data, G, RandPerm, Row, Column,nTrain, target_aug_data_ssl, target_aug_label_ssl = get_target_dataset(Data_Band_Scaler=Data_Band_Scaler,
                                                                                                              GroundTruth=GroundTruth,
                                                                                                              class_num=TAR_CLASS_NUM,
                                                                                                              tar_lsample_num_per_class=TAR_LSAMPLE_NUM_PER_CLASS,
                                                                                                              shot_num_per_class=TAR_LSAMPLE_NUM_PER_CLASS,
                                                                                                              patch_size=patch_size)

    # target SSL data
    target_ssl_dataset = tagetSSLDataset(target_aug_data_ssl, target_aug_label_ssl)
    target_ssl_dataloader = torch.utils.data.DataLoader(target_ssl_dataset,batch_size=64,shuffle=True, drop_last=True)

    num_supports, num_samples, query_edge_mask, evaluation_mask = utils.preprocess(TAR_CLASS_NUM, SHOT_NUM_PER_CLASS, QUERY_NUM_PER_CLASS, batch_task, GPU)

    '''
        用于自监督对比学习的不带标签的训练数据集（元任务就从这里边抽取）/ 用于源域粗糙类和精细类的训练数据集
        CH源域：每类200个，共18类，3600个样本，batchsize = 256, 15次迭代一个epoch，1000个episode大概66.67个epoch
        UP目标域：每类200个，共9类，1800个样本，batchsize = 128, 15次迭代一个epoch，2000个episode大概133，33个epoch
    '''
    # metatrain_data_loader_src = get_metatrain_data_loader(metatrain_data, SRC_CLASS_NUM, split='src')
    # metatrain_data_loader_tar = get_metatrain_data_loader(target_da_metatrain_data, TAR_CLASS_NUM, split='tar')

    # 模型初始化
    mapping_src = Mapping(SRC_INPUT_DIMENSION, N_DIMENSION).to(GPU)
    mapping_tar = Mapping(TAR_INPUT_DIMENSION, N_DIMENSION).to(GPU)
    encoder = Encoder(n_dimension=N_DIMENSION, patch_size=patch_size, emb_size=emb_size, dropout=0.5).to(GPU)

    # 优化器初始化
    # mapping_src_optim = torch.optim.Adam(mapping_src.parameters(), lr=LEARNING_RATE, weight_decay=WEIGHT_DECAY)
    # mapping_tar_optim = torch.optim.Adam(mapping_tar.parameters(), lr=LEARNING_RATE, weight_decay=WEIGHT_DECAY)
    # encoder_optim = torch.optim.Adam(encoder.parameters(), lr=LEARNING_RATE, weight_decay=WEIGHT_DECAY)

    mapping_src_optim = torch.optim.SGD(mapping_src.parameters(), lr=LEARNING_RATE, momentum=0.9, weight_decay=WEIGHT_DECAY)
    mapping_tar_optim = torch.optim.SGD(mapping_tar.parameters(), lr=LEARNING_RATE, momentum=0.9, weight_decay=WEIGHT_DECAY)
    encoder_optim = torch.optim.SGD(encoder.parameters(), lr=LEARNING_RATE, momentum=0.9, weight_decay=WEIGHT_DECAY)

    # 学习率衰减：每隔100个episode调整一下学习率，共调整100次
    # scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(feature_encoder_optim, T_max=100, eta_min=0.001, last_epoch=-1)

    # 参数初始化
    mapping_src.apply(utils.weights_init)
    mapping_tar.apply(utils.weights_init)
    encoder.apply(utils.weights_init)

    # why? RuntimeError: Tensor for 'out' is on CPU, Tensor for argument #1 'self' is on CPU, but expected them to be on GPU (while checking arguments for addmm)
    mapping_src.to(GPU)
    mapping_tar.to(GPU)
    encoder.to(GPU)

    # 训练模式
    mapping_src.train()
    mapping_tar.train()
    encoder.train()

    logger.info("Training...")
    last_accuracy = 0.0
    best_episode = 0
    total_hit_src, total_num_src, total_hit_tar, total_num_tar, acc_src, acc_tar = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0

    train_start = time.time()
    writer = SummaryWriter()

    target_ssl_iter = iter(target_ssl_dataloader)

    for episode in range(EPISODE) :
        # print("episode = ", episode)
        # Source and Target Few-Shot
        task_src = Task(metatrain_data, TAR_CLASS_NUM, SHOT_NUM_PER_CLASS, QUERY_NUM_PER_CLASS)
        support_dataloader_src = get_HBKC_data_loader(task_src, num_per_class=SHOT_NUM_PER_CLASS, split="train", shuffle=False)
        query_dataloader_src = get_HBKC_data_loader(task_src, num_per_class=QUERY_NUM_PER_CLASS, split="test", shuffle=False)

        task_tar = Task(target_da_metatrain_data, TAR_CLASS_NUM, SHOT_NUM_PER_CLASS, QUERY_NUM_PER_CLASS)
        support_dataloader_tar = get_HBKC_data_loader(task_tar, num_per_class=SHOT_NUM_PER_CLASS, split="train", shuffle=False)
        query_dataloader_tar = get_HBKC_data_loader(task_tar, num_per_class=QUERY_NUM_PER_CLASS, split="test", shuffle=False)

        support_src, support_label_src = support_dataloader_src.__iter__().next()  # [9, 128, 9, 9]
        query_src, query_label_src = query_dataloader_src.__iter__().next()  # (171, 128, 9, 9)
        # list
        support_real_labels_src = task_src.support_real_labels
        support_real_labels_tar = task_tar.support_real_labels

        # query_real_labels_src = task_src.query_real_labels
        # query_real_labels_tar = task_tar.query_real_labels

        semantic_support_src = torch.zeros(TAR_CLASS_NUM, 768) # (9, 768)
        for i, class_id in enumerate(support_real_labels_src) :
            semantic_support_src[i] = torch.from_numpy(semantic_mapping_src[class_id])

        # check float loss = only 4
        semantic_support_tar = torch.zeros(TAR_CLASS_NUM, 768)  # (9, 768)
        for i, class_id in enumerate(support_real_labels_tar):
            semantic_support_tar[i] = torch.from_numpy(semantic_mapping_tar[class_id])

        support_tar, support_label_tar = support_dataloader_tar.__iter__().next()  # [9, 128, 9, 9]
        query_tar, query_label_tar = query_dataloader_tar.__iter__().next()  # (171, 128, 9, 9)

        support_features_src, semantic_feature_src = encoder(mapping_src(support_src.to(GPU)), semantic_feature=semantic_support_src.to(GPU), s_or_q = "support") # (9, 160)
        query_features_src = encoder(mapping_src(query_src.to(GPU))) # (171, 160)  correct

        support_features_tar, semantic_feature_tar = encoder(mapping_tar(support_tar.to(GPU)), semantic_feature=semantic_support_tar.to(GPU), s_or_q = "support")  # (9, 160)
        query_features_tar = encoder(mapping_tar(query_tar.to(GPU)))  # (171, 160)   correct

        # # 计算模型运算量和计算量【重要】计算flops和params的时候把feature_encoder中mapping默认改成target！计算完改回默认值为source、【发现】不用改了，thop.profile可以直接把参数输入
        # print("-----------------------------------------------------------------")
        # from thop import profile
        #
        # flops1, params1 = profile(mapping_tar, inputs=(support_tar.to(GPU),))
        # # IDE_block_src.to(GPU)
        # flops2, params2 = profile(encoder, inputs=(mapping_tar(query_tar.to(GPU)), semantic_support_tar.to(GPU), "support"))
        #
        # print('FLOPs: %.2f' % ((flops1 + flops2)))
        # print('Params: %.2f' % ((params1 + params2)))
        # print('FLOPs: %.2f M' % ((flops1 + flops2) / 1e6))
        # print('Params: %.2f M' % ((params1 + params2) / 1e6))
        # print("====================================================================")


        # if episode % 20 == 0:
        #     # t_sne.t_SNE(semantic_feature_tar.cpu().detach(), torch.tensor(support_real_labels_tar))
        #     # t_sne.t_SNE(support_features_tar.cpu().detach(), torch.tensor(support_real_labels_tar))
        #     # t_sne.t_SNE(query_features_tar.cpu().detach(), torch.tensor(query_real_labels_tar))
        #
        #     cat_features = torch.cat((spatial_spectral_fusion_feature_tar, semantic_feature_tar, support_features_tar), dim=0)
        #     # cat_features = torch.cat((spatial_spectral_fusion_feature_tar, semantic_feature_tar), dim=0)
        #
        #
        #     # ss_labels = torch.zeros(TAR_CLASS_NUM)
        #     semantic_labels = torch.zeros(TAR_CLASS_NUM) + 16
        #     # fustion_labels = torch.zeros(TAR_CLASS_NUM)
        #
        #     # cat_labels = torch.cat((ss_labels, semantic_labels,fustion_labels), dim=0)
        #     cat_labels = torch.cat((torch.tensor(support_real_labels_tar), semantic_labels, torch.tensor(support_real_labels_tar) ), dim=0) # 0-15
        #
        #     t_sne.t_SNE(cat_features.cpu().detach(), torch.Tensor(cat_labels))


        # Prototype
        if SHOT_NUM_PER_CLASS > 1:
            support_proto_src = support_features_src.reshape(TAR_CLASS_NUM, SHOT_NUM_PER_CLASS, -1).mean(dim=1)  # (16, 128)
            support_proto_tar = support_features_tar.reshape(TAR_CLASS_NUM, SHOT_NUM_PER_CLASS, -1).mean(dim=1)  # (16, 128)

        else:
            support_proto_src = support_features_src
            support_proto_tar = support_features_tar

        logits_src = utils.euclidean_metric(query_features_src, support_proto_src)
        f_loss_src = crossEntropy(logits_src, query_label_src.long().to(GPU))

        logits_tar = utils.euclidean_metric(query_features_tar, support_proto_tar)
        f_loss_tar = crossEntropy(logits_tar, query_label_tar.long().to(GPU))

        f_loss = f_loss_src + f_loss_tar

        text_align_loss = infoNCE_Loss(semantic_feature_src, support_features_src) + infoNCE_Loss(semantic_feature_tar, support_features_tar)

        # # 两次增强
        # augment1_support_proto_tar = torch.FloatTensor(data_augment.random_mask_batch_image(support_tar.data.cpu(), 0.4)) # (16, 200, 7, 7)
        # augment2_support_proto_tar = torch.FloatTensor(data_augment.random_mask_batch_image(support_tar.data.cpu(), 0.4)) # (16, 200, 7, 7)
        # augment_support_proto_tar = torch.cat((augment1_support_proto_tar, augment2_support_proto_tar), dim=0)  # (32, 200, 7, 7)
        # features_augment = encoder(mapping_tar(augment_support_proto_tar.to(GPU))) # (32, 128)
        # scl_loss_tar = infoNCE_Loss_SSL(features_augment[:TAR_CLASS_NUM, :], features_augment[TAR_CLASS_NUM:, :])

        # 目标域有监督
        try:
            target_ssl_data, target_ssl_label = target_ssl_iter.next()
        except Exception as err:
            target_ssl_iter = iter(target_ssl_dataloader)
            target_ssl_data, target_ssl_label = target_ssl_iter.next()

        augment1_target_ssl_data = torch.FloatTensor(data_augment.random_mask_batch_image(target_ssl_data.data.cpu(), 0.4))  # (128, 200, 7, 7)
        augment2_target_ssl_data = torch.FloatTensor(data_augment.random_mask_batch_image(target_ssl_data.data.cpu(), 0.4))  # (128, 200, 7, 7)
        augment_target_ssl_data = torch.cat((augment1_target_ssl_data, augment2_target_ssl_data), dim=0)  # (256, 200, 7, 7)
        features_augment = encoder(mapping_tar(augment_target_ssl_data.to(GPU)))  # (256, 128)

        augment1_target_ssl_feature = F.normalize(features_augment[:len(target_ssl_data), :], dim = 1)  # (128, 128)
        augment2_target_ssl_feature = F.normalize(features_augment[len(target_ssl_data):, :], dim = 1)  # (128, 128)
        augment_target_ssl_feature = torch.cat([augment1_target_ssl_feature.unsqueeze(1), augment2_target_ssl_feature.unsqueeze(1)], dim=1) # (128, 2, 128)
        scl_loss_tar = SupConLoss_t(augment_target_ssl_feature, target_ssl_label)

        loss = f_loss + 2.0 * text_align_loss + scl_loss_tar

        # SS_CL
        # train_cl = metatrain_data_loader_src.__iter__().next()  # (256, 128, 9, 9)
        # augment1_train = torch.FloatTensor(data_augment.gaussian_noise(data_augment.Crop_and_resize_batch(train_cl.data.cpu())))
        # augment2_train = torch.FloatTensor(data_augment.gaussian_noise(data_augment.Crop_and_resize_batch(train_cl.data.cpu())))
        # features_augment1_train = encoder(mapping_src(augment1_train.to(GPU)))  # (256, 160)
        # features_augment2_train = encoder(mapping_src(augment2_train.to(GPU)))  # (256, 160)
        # p1, p2, z1, z2 = SimSiam_block_src(features_augment1_train, features_augment2_train)
        # cl_loss = -(cos_criterion(p1, z2).mean() + cos_criterion(p2, z1).mean()) * 0.5
        # loss = f_loss + lambda_1 * cl_loss

        # Update parameters
        mapping_src.zero_grad()
        mapping_tar.zero_grad()
        encoder.zero_grad()

        loss.backward()

        mapping_src_optim.step()
        mapping_tar_optim.step()
        encoder_optim.step()


        total_hit_src += torch.sum(torch.argmax(logits_src, dim=1).cpu() == query_label_src).item()
        total_num_src += query_src.shape[0]
        acc_src = total_hit_src / total_num_src

        total_hit_tar += torch.sum(torch.argmax(logits_tar, dim=1).cpu() == query_label_tar).item()
        total_num_tar += query_tar.shape[0]
        acc_tar = total_hit_tar / total_num_tar

        if (episode + 1) % 100 == 0:
            # tensor.item() 把张量转换为python标准数字返回，仅适用只有一个元素的张量。
            logger.info('episode: {:>3d}, f_loss: {:6.4f}, text_align_loss: {:6.4f}, scl_loss_tar: {:6.4f}, loss: {:6.4f}, acc_src: {:6.4f}, acc_tar: {:6.4f}'.format(
                episode + 1,
                f_loss.item(),
                text_align_loss.item(),
                scl_loss_tar.item(),
                loss.item(),
                acc_src,
                acc_tar))

            # writer.add_scalar('Loss/loss_c', loss_c.item(), episode + 1)  # 名字 y x
            writer.add_scalar('Loss/f_loss', f_loss.item(), episode + 1)
            writer.add_scalar('Loss/text_align_loss', text_align_loss.item(), episode + 1)
            writer.add_scalar('Loss/scl_loss_tar', scl_loss_tar.item(), episode + 1)
            writer.add_scalar('Loss/loss', loss.item(), episode + 1)

            writer.add_scalar('Acc/acc_src', acc_src, episode + 1)
            writer.add_scalar('Acc/acc_tar', acc_tar, episode + 1)

            # writer.add_scalar('Optimizer/lr',feature_encoder_optim.param_groups[0]['lr'], episode + 1)
            # 每隔100个调整一下feature_encoder的学习率,共调整100次
            # scheduler.step()
        if (episode + 1) % 500 == 0 or episode == 0:
            with torch.no_grad():
                # test
                logger.info("Testing ...")
                train_end = time.time()
                mapping_tar.eval()
                encoder.eval()
                total_rewards = 0
                counter = 0
                accuracies = []
                predict = np.array([], dtype=np.int64)
                predict_gnn = np.array([], dtype=np.int64)
                labels = np.array([], dtype=np.int64)

                train_datas, train_labels = train_loader.__iter__().next()

                support_real_labels = train_labels

                semantic_support = torch.zeros(TAR_CLASS_NUM*TAR_LSAMPLE_NUM_PER_CLASS, 768)  # (9*5=45, 768)
                for i, class_id in enumerate(support_real_labels):
                    semantic_support[i] = torch.from_numpy(semantic_mapping_tar[class_id])

                train_features, _ = encoder(mapping_tar(Variable(train_datas).to(GPU)), semantic_feature = semantic_support.to(GPU),  s_or_q = "support")

                max_value = train_features.max()
                min_value = train_features.min()
                print(max_value.item())
                print(min_value.item())
                train_features = (train_features - min_value) * 1.0 / (max_value - min_value)

                KNN_classifier = KNeighborsClassifier(n_neighbors=1)
                KNN_classifier.fit(train_features.cpu().detach().numpy(), train_labels)
                for test_datas, test_labels in test_loader:
                    batch_size = test_labels.shape[0]

                    test_features = encoder(mapping_tar((Variable(test_datas).to(GPU))))

                    test_features = (test_features - min_value) * 1.0 / (max_value - min_value)
                    predict_labels = KNN_classifier.predict(test_features.cpu().detach().numpy())
                    test_labels = test_labels.numpy()
                    rewards = [1 if predict_labels[j] == test_labels[j] else 0 for j in range(batch_size)]

                    total_rewards += np.sum(rewards)
                    counter += batch_size

                    predict = np.append(predict, predict_labels)
                    labels = np.append(labels, test_labels)

                    accuracy = total_rewards / 1.0 / counter
                    accuracies.append(accuracy)

                test_accuracy = 100. * total_rewards / len(test_loader.dataset)
                writer.add_scalar('Acc/acc_test', test_accuracy, episode + 1)

                logger.info('\t\tAccuracy: {}/{} ({:.2f}%)\n'.format(total_rewards, len(test_loader.dataset), 100. * total_rewards / len(test_loader.dataset)))
                test_end = time.time()

                # Training mode
                mapping_tar.train()
                encoder.train()
                if test_accuracy > last_accuracy:
                    last_accuracy = test_accuracy
                    best_episode = episode
                    acc[iDataSet] = 100. * total_rewards / len(test_loader.dataset)
                    OA = acc
                    C = metrics.confusion_matrix(labels, predict)
                    A[iDataSet, :] = np.diag(C) / np.sum(C, 1, dtype=float)
                    best_predict_all = predict
                    best_G, best_RandPerm, best_Row, best_Column, best_nTrain = G, RandPerm, Row, Column, nTrain
                    k[iDataSet] = metrics.cohen_kappa_score(labels, predict)

                logger.info('best episode:[{}], best accuracy={}'.format(best_episode + 1, last_accuracy))

    logger.info('iter:{} best episode:[{}], best accuracy={}'.format(iDataSet, best_episode + 1, last_accuracy))
    logger.info ("train time per DataSet(s): " + "{:.5f}".format(train_end-train_start))
    logger.info("accuracy list: {}".format(acc))
    logger.info('***********************************************************************************')


OAMean = np.mean(acc)
OAStd = np.std(acc)

AA = np.mean(A, 1)
AAMean = np.mean(AA,0)
AAStd = np.std(AA)

kMean = np.mean(k)
kStd = np.std(k)

AMean = np.mean(A, 0)
AStd = np.std(A, 0)

logger.info ("train time per DataSet(s): " + "{:.5f}".format(train_end-train_start)) # 这个不是平均时间！是最后一个种子的时间！
logger.info ("test time per DataSet(s): " + "{:.5f}".format(test_end-train_end)) # 这个不是平均时间！是最后一个种子的时间！
logger.info ("average OA: " + "{:.2f}".format(OAMean) + " +- " + "{:.2f}".format( OAStd))
logger.info ("average AA: " + "{:.2f}".format(100 * AAMean) + " +- " + "{:.2f}".format(100 * AAStd))
logger.info ("average kappa: " + "{:.4f}".format(100 *kMean) + " +- " + "{:.4f}".format(100 *kStd))
logger.info ("accuracy list: {}".format(acc))
logger.info ("accuracy for each class: ")
for i in range(TAR_CLASS_NUM):
    logger.info ("Class " + str(i) + ": " + "{:.2f}".format(100 * AMean[i]) + " +- " + "{:.2f}".format(100 * AStd[i]))


#################classification map################################

# # G一直是GT值，所以best_G也是预测值，需要重新赋值成预测的结果
# for i in range(len(best_predict_all)):  # 12197
#     best_G[best_Row[best_RandPerm[best_nTrain + i]]][best_Column[best_RandPerm[best_nTrain + i]]] = best_predict_all[i] + 1
#
# hsi_pic = np.zeros((best_G.shape[0], best_G.shape[1], 3))
# for i in range(best_G.shape[0]):
#     for j in range(best_G.shape[1]):
#         if best_G[i][j] == 0:
#             hsi_pic[i, j, :] = [0, 0, 0]
#         if best_G[i][j] == 1:
#             hsi_pic[i, j, :] = [0, 0, 1]
#         if best_G[i][j] == 2:
#             hsi_pic[i, j, :] = [0, 1, 0]
#         if best_G[i][j] == 3:
#             hsi_pic[i, j, :] = [0, 1, 1]
#         if best_G[i][j] == 4:
#             hsi_pic[i, j, :] = [1, 0, 0]
#         if best_G[i][j] == 5:
#             hsi_pic[i, j, :] = [1, 0, 1]
#         if best_G[i][j] == 6:
#             hsi_pic[i, j, :] = [1, 1, 0]
#         if best_G[i][j] == 7:
#             hsi_pic[i, j, :] = [0.5, 0.5, 1]
#         if best_G[i][j] == 8:
#             hsi_pic[i, j, :] = [0.65, 0.35, 1]
#         if best_G[i][j] == 9:
#             hsi_pic[i, j, :] = [0.75, 0.5, 0.75]
#
# # 4 指的是halfwidth
# halfwidth = patch_size // 2
# utils.classification_map(hsi_pic[halfwidth:-halfwidth, halfwidth:-halfwidth, :], best_G[halfwidth:-halfwidth, halfwidth:-halfwidth], 24,  "classificationMap/LK_{}shot.png".format(TAR_LSAMPLE_NUM_PER_CLASS))
#




