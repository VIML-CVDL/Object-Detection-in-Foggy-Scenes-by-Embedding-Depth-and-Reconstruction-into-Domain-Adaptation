# --------------------------------------------------------
# Tensorflow Faster R-CNN
# Licensed under The MIT License [see LICENSE for details]
# Written by Xinlei Chen
# --------------------------------------------------------
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from model.utils.config import cfg
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import math
import torchvision.models as models
from model.da_faster_rcnn.faster_rcnn import _fasterRCNN
import pdb
from model.utils.net_utils import _smooth_l1_loss, _crop_pool_layer, _affine_grid_gen, _affine_theta
from model.da_faster_rcnn.DA import _ImageDA
from model.da_faster_rcnn.DA import _InstanceDA
from model.da_faster_rcnn.DA import grad_reverse

def conv3x3(in_planes, out_planes, stride=1, padding=0):
  "3x3 convolution with padding"
  return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
           padding=padding, bias=False)
def conv1x1(in_planes, out_planes, stride=1):
  "3x3 convolution with padding"
  return nn.Conv2d(in_planes, out_planes, kernel_size=1, stride=stride,
           padding=0, bias=False)

class PEN(nn.Module):
    def __init__(self):
        super(PEN, self).__init__()
        self.conv1 = conv1x1(512, 128)
        self.bn1 = nn.BatchNorm2d(128)
        self.conv2 = conv3x3(128, 64, padding=1)
        self.bn2 = nn.BatchNorm2d(64)
        self.conv3 = conv3x3(64, 64, padding=1)
        self.bn3 = nn.BatchNorm2d(64)
        self.conv4 = conv3x3(64, 3, padding=1)
        self._init_weights()

    def _init_weights(self):
      def normal_init(m, mean, stddev, truncated=False):
        """
        weight initalizer: truncated normal and random normal.
        """
        # x is a parameter
        if truncated:
          m.weight.data.normal_().fmod_(2).mul_(stddev).add_(mean)  # not a perfect approximation
        else:
          m.weight.data.normal_(mean, stddev)
          #m.bias.data.zero_()
      normal_init(self.conv1, 0, 0.01)
      normal_init(self.conv2, 0, 0.01)
      normal_init(self.conv3, 0, 0.01)
      normal_init(self.conv4, 0, 0.01)

    def forward(self, x):
        x = grad_reverse(x)
        x = self.conv1(x)
        x = F.relu(self.bn1(x))
        x = self.conv2(x)
        x = F.relu(self.bn2(x))
        x = self.conv3(x)
        x = F.relu(self.bn3(x))
        x = F.sigmoid(self.conv4(x))

        return x

class DEN(nn.Module):
    def __init__(self):
        super(DEN, self).__init__()
        self.conv1 = conv1x1(512, 128)
        self.bn1 = nn.BatchNorm2d(128)
        self.conv2 = conv3x3(128, 64, padding=1)
        self.bn2 = nn.BatchNorm2d(64)
        self.conv3 = conv3x3(64, 64, padding=1)
        self.bn3 = nn.BatchNorm2d(64)
        self.conv4 = conv3x3(64, 3, padding=1)
        self._init_weights()

    def _init_weights(self):
      def normal_init(m, mean, stddev, truncated=False):
        """
        weight initalizer: truncated normal and random normal.
        """
        # x is a parameter
        if truncated:
          m.weight.data.normal_().fmod_(2).mul_(stddev).add_(mean)  # not a perfect approximation
        else:
          m.weight.data.normal_(mean, stddev)
          #m.bias.data.zero_()
      normal_init(self.conv1, 0, 0.01)
      normal_init(self.conv2, 0, 0.01)
      normal_init(self.conv3, 0, 0.01)
      normal_init(self.conv4, 0, 0.01)

    def forward(self, x):
        x = self.conv1(x)
        x = F.relu(self.bn1(x))
        x = self.conv2(x)
        x = F.relu(self.bn2(x))
        x = self.conv3(x)
        x = F.relu(self.bn3(x))
        x = F.sigmoid(self.conv4(x))

        return x

class vgg16_transmission_depth_cst(_fasterRCNN):
  def __init__(self, classes, pretrained=False, class_agnostic=False):
    self.model_path = 'data/pretrained_model/vgg16_caffe.pth'
    self.dout_base_model = 512
    self.pretrained = pretrained
    self.class_agnostic = class_agnostic

    _fasterRCNN.__init__(self, classes, class_agnostic)

  def _init_modules(self):
    vgg = models.vgg16()
    if self.pretrained:
        print("Loading pretrained weights from %s" %(self.model_path))
        state_dict = torch.load(self.model_path)
        vgg.load_state_dict({k:v for k,v in state_dict.items() if k in vgg.state_dict()})

    vgg.classifier = nn.Sequential(*list(vgg.classifier._modules.values())[:-1])

    # not using the last maxpool layer
    self.RCNN_base = nn.Sequential(*list(vgg.features._modules.values())[:-1])

    self.pen = PEN()
    self.den = DEN()
    # Fix the layers before conv3:
    for layer in range(10):
      for p in self.RCNN_base[layer].parameters(): p.requires_grad = False

    # self.RCNN_base = _RCNN_base(vgg.features, self.classes, self.dout_base_model)

    self.RCNN_top = vgg.classifier

    # not using the last maxpool layer
    self.RCNN_cls_score = nn.Linear(4096, self.n_classes)

    if self.class_agnostic:
      self.RCNN_bbox_pred = nn.Linear(4096, 4)
    else:
      self.RCNN_bbox_pred = nn.Linear(4096, 4 * self.n_classes)

  def forward(self, im_data, im_info, gt_boxes, num_boxes, need_backprop,
            tgt_im_data, tgt_im_info, tgt_gt_boxes, tgt_num_boxes, tgt_need_backprop, transmission_map=None, tgt_transmission_map=None, depth=None, tgt_depth=None):

    assert need_backprop.detach()==1 and tgt_need_backprop.detach()==0

    batch_size = im_data.size(0)
    im_info = im_info.data     #(size1,size2, image ratio(new image / source image) )
    gt_boxes = gt_boxes.data
    num_boxes = num_boxes.data
    need_backprop=need_backprop.data

    # feed image data to base model to obtain base feature map
    base_feat = self.RCNN_base(im_data)
    prior = self.pen(base_feat)

    # feed base feature map tp RPN to obtain rois
    self.RCNN_rpn.train()
    rois, rpn_loss_cls, rpn_loss_bbox = self.RCNN_rpn(base_feat, im_info, gt_boxes, num_boxes)

    # if it is training phrase, then use ground trubut bboxes for refining
    if self.training:
        roi_data = self.RCNN_proposal_target(rois, gt_boxes, num_boxes)
        rois, rois_label, rois_target, rois_inside_ws, rois_outside_ws = roi_data

        rois_label = Variable(rois_label.view(-1).long())
        rois_target = Variable(rois_target.view(-1, rois_target.size(2)))
        rois_inside_ws = Variable(rois_inside_ws.view(-1, rois_inside_ws.size(2)))
        rois_outside_ws = Variable(rois_outside_ws.view(-1, rois_outside_ws.size(2)))
    else:
        rois_label = None
        rois_target = None
        rois_inside_ws = None
        rois_outside_ws = None
        rpn_loss_cls = 0
        rpn_loss_bbox = 0

    rois = Variable(rois)
    # do roi pooling based on predicted rois

    if cfg.POOLING_MODE == 'crop':
        # pdb.set_trace()
        # pooled_feat_anchor = _crop_pool_layer(base_feat, rois.view(-1, 5))
        grid_xy = _affine_grid_gen(rois.view(-1, 5), base_feat.size()[2:], self.grid_size)
        grid_yx = torch.stack([grid_xy.data[:,:,:,1], grid_xy.data[:,:,:,0]], 3).contiguous()
        pooled_feat = self.RCNN_roi_crop(base_feat, Variable(grid_yx).detach())
        if cfg.CROP_RESIZE_WITH_MAX_POOL:
            pooled_feat = F.max_pool2d(pooled_feat, 2, 2)
    elif cfg.POOLING_MODE == 'align':
        pooled_feat = self.RCNN_roi_align(base_feat, rois.view(-1, 5))
    elif cfg.POOLING_MODE == 'pool':
        pooled_feat = self.RCNN_roi_pool(base_feat, rois.view(-1,5))

    # feed pooled features to top model
    pooled_feat = self._head_to_tail(pooled_feat)

    # compute bbox offset
    bbox_pred = self.RCNN_bbox_pred(pooled_feat)
    if self.training and not self.class_agnostic:
        # select the corresponding columns according to roi labels
        bbox_pred_view = bbox_pred.view(bbox_pred.size(0), int(bbox_pred.size(1) / 4), 4)
        bbox_pred_select = torch.gather(bbox_pred_view, 1, rois_label.view(rois_label.size(0), 1, 1).expand(rois_label.size(0), 1, 4))
        bbox_pred = bbox_pred_select.squeeze(1)

    # compute object classification probability
    cls_score = self.RCNN_cls_score(pooled_feat)
    cls_prob = F.softmax(cls_score, 1)

    RCNN_loss_cls = 0
    RCNN_loss_bbox = 0

    if self.training:
        # classification loss
        RCNN_loss_cls = F.cross_entropy(cls_score, rois_label)

        # bounding box regression L1 loss
        RCNN_loss_bbox = _smooth_l1_loss(bbox_pred, rois_target, rois_inside_ws, rois_outside_ws)


    cls_prob = cls_prob.view(batch_size, rois.size(1), -1)
    bbox_pred = bbox_pred.view(batch_size, rois.size(1), -1)

    """ =================== for target =========================="""

    tgt_batch_size = tgt_im_data.size(0)
    tgt_im_info = tgt_im_info.data  # (size1,size2, image ratio(new image / source image) )
    tgt_gt_boxes = tgt_gt_boxes.data
    tgt_num_boxes = tgt_num_boxes.data
    tgt_need_backprop = tgt_need_backprop.data

    # feed image data to base model to obtain base feature map
    tgt_base_feat = self.RCNN_base(tgt_im_data)
    tgt_prior = self.pen(tgt_base_feat)

    # feed base feature map tp RPN to obtain rois
    self.RCNN_rpn.eval()
    tgt_rois, tgt_rpn_loss_cls, tgt_rpn_loss_bbox = \
        self.RCNN_rpn(tgt_base_feat, tgt_im_info, tgt_gt_boxes, tgt_num_boxes)

    ploss = nn.MSELoss()
    prior_loss = ploss(prior, transmission_map)
    prior_loss += ploss(tgt_prior, tgt_transmission_map.squeeze(0).permute([0,3,1,2]))


    dep = self.den(base_feat)
    tgt_dep = self.den(tgt_base_feat)

    dloss = nn.MSELoss()
    depth_loss = dloss(dep, depth)

    cstloss = nn.MSELoss()
    cst_loss = cstloss(-torch.log(tgt_prior), 1-tgt_dep.detach())

    return rois, cls_prob, bbox_pred, rpn_loss_cls, rpn_loss_bbox, RCNN_loss_cls, RCNN_loss_bbox, rois_label, prior_loss, depth_loss, cst_loss

  def _head_to_tail(self, pool5):

    pool5_flat = pool5.view(pool5.size(0), -1)
    fc7 = self.RCNN_top(pool5_flat)

    return fc7
