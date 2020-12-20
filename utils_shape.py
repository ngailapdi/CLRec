import numpy as np
import os
import config
from datetime import datetime
import copy
import torch
from mesh_gen_utils.libmise import MISE
from mesh_gen_utils.libmesh import check_mesh_contains
from mesh_gen_utils import libmcubes
import trimesh
from mesh_gen_utils.libkdtree import KDTree
from torch.autograd import Variable
import h5py
import torch.nn as nn
import struct
import pymesh
from PIL import Image




def writelogfile(log_dir):
    log_file_name = os.path.join(log_dir, 'log.txt')
    with open(log_file_name, "a+") as log_file:
        log_string = get_log_string()
        log_file.write(log_string)


def get_log_string():
    now = str(datetime.now().strftime("%H:%M %d-%m-%Y"))
    log_string = ""
    log_string += " -------- Hyperparameters and settings -------- \n"
    log_string += "{:25} {}\n".format('Time:', now)
    log_string += "{:25} {}\n".format('Mini-batch size:', \
        config.training['batch_size'])
    log_string += "{:25} {}\n".format('Batch size eval:', \
        config.training['batch_size_eval'])
    log_string += "{:25} {}\n".format('Num epochs:', \
        config.training['num_epochs'])
    log_string += "{:25} {}\n".format('Out directory:', \
        config.training['out_dir'])
    log_string += "{:25} {}\n".format('Random view:', \
        config.data_setting['random_view'])
    log_string += "{:25} {}\n".format('Sequence length:', \
        config.data_setting['seq_len'])
    log_string += "{:25} {}\n".format('Input size:', \
        config.data_setting['input_size'])
    log_string += " -------- Data paths -------- \n"
    log_string += "{:25} {}\n".format('Dataset path', \
        config.path['src_dataset_path'])
    log_string += "{:25} {}\n".format('Point path', \
        config.path['src_pt_path'])
    log_string += " ------------------------------------------------------ \n"
    return log_string




def compute_iou(occ1, occ2):
    ''' Computes the Intersection over Union (IoU) value for two sets of
    occupancy values.

    Args:
        occ1 (tensor): first set of occupancy values
        occ2 (tensor): second set of occupancy values
    '''
    occ1 = np.asarray(occ1)
    occ2 = np.asarray(occ2)

    # Put all data in second dimension
    # Also works for 1-dimensional data
    if occ1.ndim >= 2:
        occ1 = occ1.reshape(occ1.shape[0], -1)
    if occ2.ndim >= 2:
        occ2 = occ2.reshape(occ2.shape[0], -1)

    # Convert to boolean values
    occ1_temp = copy.deepcopy(occ1)
    occ2_temp = copy.deepcopy(occ2)
    occ1 = (occ1 >= 0.5)
    occ2 = (occ2 >= 0.5)

    # Compute IOU
    area_union = (occ1 | occ2).astype(np.float32).sum(axis=-1)
    if (area_union == 0).any():
        # import pdb; pdb.set_trace()
        return 0.

    area_intersect = (occ1 & occ2).astype(np.float32).sum(axis=-1)

    iou = (area_intersect / area_union)
    if isinstance(iou, (list,np.ndarray)):
        iou = np.mean(iou, axis=0)
    return iou

def compute_acc(sdf_pred, sdf, thres=0.01, iso=0.003):
    # import pdb; pdb.set_trace()
    sdf_pred = np.asarray(sdf_pred)
    sdf = np.asarray(sdf)

    acc_sign = (((sdf_pred-iso) * (sdf-iso)) > 0).mean(axis=-1)
    acc_sign = np.mean(acc_sign, axis=0)

    occ_pred = sdf_pred <= iso
    occ = sdf <= iso

    iou = compute_iou(occ_pred, occ)

    acc_thres = (np.abs(sdf_pred-sdf) <= thres).mean(axis=-1)
    acc_thres = np.mean(acc_thres, axis=0)
    return acc_sign, acc_thres, iou

def get_sdf_h5(sdf_h5_file):
    h5_f = h5py.File(sdf_h5_file, 'r')
    try:
        if ('pc_sdf_original' in h5_f.keys()
                and 'pc_sdf_sample' in h5_f.keys()
                and 'norm_params' in h5_f.keys()):
            ori_sdf = h5_f['pc_sdf_original'][:].astype(np.float32)
            sample_sdf = h5_f['pc_sdf_sample'][:].astype(np.float32)
            ori_pt = ori_sdf[:,:3]#, ori_sdf[:,3]
            ori_sdf_val = None
            if sample_sdf.shape[1] == 4:
                sample_pt, sample_sdf_val = sample_sdf[:,:3], sample_sdf[:,3]
            else:
                sample_pt, sample_sdf_val = None, sample_sdf[:, 0]
            norm_params = h5_f['norm_params'][:]
            sdf_params = h5_f['sdf_params'][:]
        else:
            raise Exception("no sdf and sample")
    finally:
        h5_f.close()
    return ori_pt, ori_sdf_val, sample_pt, sample_sdf_val, norm_params, sdf_params

def get_sdf_h5_occ(occ_h5_file):
    h5_f = h5py.File(occ_h5_file, 'r')
    try:
        if 'occ' in h5_f.keys() and 'loc' in h5_f.keys():
            occ = h5_f['occ'][:].astype(np.float32)
            points = h5_f['loc'][:].astype(np.float32)
        else:
            raise Exception('no occ')
    finally:
        h5_f.close()
    return points, occ

def apply_rotate(input_points, rotate_dict):
    theta_azim = rotate_dict['azim']
    theta_elev = rotate_dict['elev']
    theta_azim = np.pi+theta_azim/180*np.pi
    theta_elev = theta_elev/180*np.pi
    r_elev = np.array([[1,       0,          0],
                        [0, np.cos(theta_elev), -np.sin(theta_elev)],
                        [0, np.sin(theta_elev), np.cos(theta_elev)]])
    r_azim = np.array([[np.cos(theta_azim), 0, np.sin(theta_azim)],
                        [0,               1,       0],
                        [-np.sin(theta_azim),0, np.cos(theta_azim)]])

    rotated_points = r_elev@r_azim@input_points.T
    return rotated_points.T

def sample_points(input_points, input_occs, num_points):
    if num_points != -1:
        idx = torch.randint(len(input_points), size=(num_points,))
    else:
        idx = torch.arange(len(input_points))
    selected_points = input_points[idx, :]
    selected_occs = input_occs[idx]
    return selected_points, selected_occs

def normalize_imagenet(x):
    ''' Normalize input images according to ImageNet standards.
    Args:
        x (tensor): input images
    '''
    x = x.clone()
    x[:, 0] = (x[:, 0] - 0.485) / 0.229
    x[:, 1] = (x[:, 1] - 0.456) / 0.224
    x[:, 2] = (x[:, 2] - 0.406) / 0.225
    return x

def LpLoss(logits, sdf, p=1, thres=0.01, weight=4.):

    sdf = Variable(sdf.data, requires_grad=False).cuda()
    loss = torch.abs(logits-sdf).pow(p).cuda()
    weight_mask = torch.ones(loss.shape).cuda()
    weight_mask[torch.abs(sdf) < thres] =\
             weight_mask[torch.abs(sdf) < thres]*weight 
    loss = loss * weight_mask
    loss = torch.sum(loss, dim=-1, keepdim=False)
    loss = torch.mean(loss)
    return loss

def LpLoss_BCE(logits, sdf, p=1, thres=0.01, weight=4., iso=0.003):

    sdf = Variable(sdf.data, requires_grad=False).cuda()
    loss_sdf = torch.abs(logits-sdf).pow(p).cuda()
    weight_mask = torch.ones(loss_sdf.shape).cuda()
    weight_mask[torch.abs(sdf) < thres] =\
             weight_mask[torch.abs(sdf) < thres]*weight 
    loss_sdf = loss_sdf * weight_mask
    loss_sdf = torch.sum(loss_sdf, dim=-1, keepdim=False)
    loss_sdf = torch.mean(loss_sdf)
    # import pdb; pdb.set_trace()

    loss_BCE_func = nn.BCELoss(reduction='none')
    sign = ((logits-iso)*(sdf-iso) <= 0).to(dtype=torch.float32).cuda()
    sign_gt = (sdf <= iso).to(dtype=torch.float32).cuda()
    loss_BCE = loss_BCE_func(sign, sign_gt)/27.631
    loss_BCE = torch.sum(loss_BCE, dim=-1, keepdim=False)
    loss_BCE = torch.mean(loss_BCE)
    loss = loss_sdf + loss_BCE
    return loss


def generate_mesh(img, points, model, threshold=0.2, box_size=1.7, \
            resolution0=16, upsampling_steps=2):
    # import pdb; pdb.set_trace()
    model.eval()

    threshold = np.log(threshold) - np.log(1. - threshold)
    mesh_extractor = MISE(
        resolution0, upsampling_steps, threshold)
    p = mesh_extractor.query()

    with torch.no_grad():
        feats = model.encoder(img)

    while p.shape[0] != 0:
        pq = torch.FloatTensor(p).cuda()
        pq = pq / mesh_extractor.resolution

        pq = box_size * (pq - 0.5)

        with torch.no_grad():
            pq = pq.unsqueeze(0)
            occ_pred = model.decoder(pq, feats)
        values = occ_pred.squeeze(0).detach().cpu().numpy()
        values = values.astype(np.float64)
        mesh_extractor.update(p, values)

        p = mesh_extractor.query()
    value_grid = mesh_extractor.to_dense()

    mesh = extract_mesh(value_grid, feats, box_size, threshold)
    return mesh

def extract_mesh(value_grid, feats, box_size, threshold, constant_values=-1e6):
    # import pdb; pdb.set_trace()
    n_x, n_y, n_z = value_grid.shape
    value_grid_padded = np.pad(
            value_grid, 1, 'constant', constant_values=constant_values)
    vertices, triangles = libmcubes.marching_cubes(
            value_grid_padded, threshold)
    # Shift back vertices by 0.5
    vertices -= 0.5
    # Undo padding
    vertices -= 1
    # Normalize
    vertices /= np.array([n_x-1, n_y-1, n_z-1])
    vertices = box_size * (vertices - 0.5)

    # Create mesh
    mesh = trimesh.Trimesh(vertices, triangles, process=False)

    return mesh

def eval_mesh(mesh, pointcloud_gt, normals_gt, points, val_gt, \
                num_fscore_thres=6, n_points=300000, algo='occnet', \
                sdf_val=None, iso=0.003):

    if mesh is not None and type(mesh)==trimesh.base.Trimesh and len(mesh.vertices) != 0 and len(mesh.faces) != 0:
        pointcloud, idx = mesh.sample(n_points, return_index=True)
        pointcloud = pointcloud.astype(np.float32)
        normals = mesh.face_normals[idx]
    else:
        if algo == 'occnet':
            return {'iou': 0., 'cd': 2*np.sqrt(3), 'completeness': np.sqrt(3),\
                    'accuracy': np.sqrt(3), 'normals_completeness': -1,\
                    'normals_accuracy': -1, 'normals': -1, \
                    'fscore': np.zeros(6, dtype=np.float32), \
                    'precision': np.zeros(6, dtype=np.float32), \
                    'recall': np.zeros(6, dtype=np.float32)}
        return {'iou': [0.,0.], 'cd': 2*np.sqrt(3), 'completeness': np.sqrt(3),\
                    'accuracy': np.sqrt(3), 'normals_completeness': -1,\
                    'normals_accuracy': -1, 'normals': -1, \
                    'fscore': np.zeros(6, dtype=np.float32), \
                    'precision': np.zeros(6, dtype=np.float32), \
                    'recall': np.zeros(6, dtype=np.float32)}
    # Eval pointcloud
    pointcloud = np.asarray(pointcloud)
    pointcloud_gt = np.asarray(pointcloud_gt.squeeze(0))
    normals = np.asarray(normals)
    normals_gt = np.asarray(normals_gt.squeeze(0))

    ####### Normalize
    pointcloud /= (2*np.max(np.abs(pointcloud)))
    pointcloud_gt /= (2*np.max(np.abs(pointcloud_gt)))

    # Completeness: how far are the points of the target point cloud
    # from thre predicted point cloud
    completeness, normals_completeness = distance_p2p(
            pointcloud_gt, normals_gt, pointcloud, normals)

    # Accuracy: how far are th points of the predicted pointcloud
    # from the target pointcloud
    accuracy, normals_accuracy = distance_p2p(
        pointcloud, normals, pointcloud_gt, normals_gt
    )

    # Get fscore
    fscore_array, precision_array, recall_array = [], [], []
    for i, thres in enumerate([0.5, 1, 2, 5, 10, 20]):
        fscore, precision, recall = calculate_fscore(\
            accuracy, completeness, thres/100.)
        fscore_array.append(fscore)
        precision_array.append(precision)
        recall_array.append(recall)
    fscore_array = np.array(fscore_array, dtype=np.float32)
    precision_array = np.array(precision_array, dtype=np.float32)
    recall_array = np.array(recall_array, dtype=np.float32)

    # import pdb; pdb.set_trace()

    accuracy = accuracy.mean()
    normals_accuracy = normals_accuracy.mean()

    completeness = completeness.mean()
    normals_completeness = normals_completeness.mean()

    cd = completeness + accuracy
    normals = 0.5*(normals_completeness+normals_accuracy)

    # Compute IoU
    if algo == 'occnet':
        occ_mesh = check_mesh_contains(mesh, points.cpu().numpy().squeeze(0))
        iou = compute_iou(occ_mesh, val_gt.cpu().numpy().squeeze(0))
    else:
        # import pdb; pdb.set_trace()

        occ_mesh = check_mesh_contains(mesh, points.cpu().numpy().squeeze(0))
        val_gt_np = val_gt.cpu().numpy()
        occ_gt = val_gt_np <= iso
        iou = compute_iou(occ_mesh, occ_gt) 

        # sdf iou
        sdf_iou, _, _ = compute_acc(sdf_val.cpu().numpy(),\
                        val_gt.cpu().numpy()) 
        iou = np.array([iou, sdf_iou])

    return {'iou': iou, 'cd': cd, 'completeness': completeness,\
                'accuracy': accuracy, \
                'normals_completeness': normals_completeness,\
                'normals_accuracy': normals_accuracy, 'normals': normals, \
                'fscore': fscore_array, 'precision': precision_array,\
                'recall': recall_array}

def eval_mesh_genre(mesh, pointcloud_gt, normals_gt, points, val_gt, \
                num_fscore_thres=6, n_points=300000, algo='occnet', \
                sdf_val=None, iso=0.003):
    if mesh is not None and type(mesh)==trimesh.base.Trimesh and len(mesh.vertices) != 0 and len(mesh.faces) != 0:
        pointcloud, idx = mesh.sample(n_points, return_index=True)
        pointcloud = pointcloud.astype(np.float32)
        normals = mesh.face_normals[idx]
    else:
        return {'iou': 0., 'cd': 2*np.sqrt(3), 'completeness': np.sqrt(3),\
                    'accuracy': np.sqrt(3), 'normals_completeness': -1,\
                    'normals_accuracy': -1, 'normals': -1, \
                    'fscore': np.zeros(6, dtype=np.float32), \
                    'precision': np.zeros(6, dtype=np.float32), \
                    'recall': np.zeros(6, dtype=np.float32)}
    # Eval pointcloud
    pointcloud = np.asarray(pointcloud)
    pointcloud_gt = np.asarray(pointcloud_gt)
    normals = np.asarray(normals)
    normals_gt = np.asarray(normals_gt)

    # Completeness: how far are the points of the target point cloud
    # from thre predicted point cloud
    completeness, normals_completeness = distance_p2p(
            pointcloud_gt, normals_gt, pointcloud, normals)

    # Accuracy: how far are th points of the predicted pointcloud
    # from the target pointcloud
    accuracy, normals_accuracy = distance_p2p(
        pointcloud, normals, pointcloud_gt, normals_gt
    )

    # Get fscore
    fscore_array, precision_array, recall_array = [], [], []
    for i, thres in enumerate([0.5, 1, 2, 5, 10, 20]):
        fscore, precision, recall = calculate_fscore(\
            accuracy, completeness, thres/100.)
        fscore_array.append(fscore)
        precision_array.append(precision)
        recall_array.append(recall)
    fscore_array = np.array(fscore_array, dtype=np.float32)
    precision_array = np.array(precision_array, dtype=np.float32)
    recall_array = np.array(recall_array, dtype=np.float32)

    # import pdb; pdb.set_trace()

    accuracy = accuracy.mean()
    normals_accuracy = normals_accuracy.mean()

    completeness = completeness.mean()
    normals_completeness = normals_completeness.mean()

    cd = completeness + accuracy
    normals = 0.5*(normals_completeness+normals_accuracy)

    # Compute IoU
    if algo == 'occnet':
        occ_mesh = check_mesh_contains(mesh, points)
        iou = compute_iou(occ_mesh, val_gt)
    else:
        # import pdb; pdb.set_trace()

        occ_mesh = check_mesh_contains(mesh, points)
        val_gt_np = val_gt
        occ_gt = val_gt_np <= iso
        iou = compute_iou(occ_mesh, occ_gt) 

        # sdf iou
        # sdf_iou, _, _ = compute_acc(sdf_val,val_gt) 
        iou = np.array([iou, 0.])

    return {'iou': iou, 'cd': cd, 'completeness': completeness,\
                'accuracy': accuracy, \
                'normals_completeness': normals_completeness,\
                'normals_accuracy': normals_accuracy, 'normals': normals, \
                'fscore': fscore_array, 'precision': precision_array,\
                'recall': recall_array}

def calculate_fscore(accuracy, completeness, threshold):
    recall = np.sum(completeness < threshold)/len(completeness)
    precision = np.sum(accuracy < threshold)/len(accuracy)
    if precision + recall > 0:
        fscore = 2*recall*precision/(recall+precision)
    else:
        fscore = 0
    return fscore, precision, recall


def distance_p2p(points_src, normals_src, points_tgt, normals_tgt):
    ''' Computes minimal distances of each point in points_src to points_tgt.

    Args:
        points_src (numpy array): source points
        normals_src (numpy array): source normals
        points_tgt (numpy array): target points
        normals_tgt (numpy array): target normals
    '''
    kdtree = KDTree(points_tgt)
    dist, idx = kdtree.query(points_src)

    if normals_src is not None and normals_tgt is not None:
        normals_src = \
            normals_src / np.linalg.norm(normals_src, axis=-1, keepdims=True)
        normals_tgt = \
            normals_tgt / np.linalg.norm(normals_tgt, axis=-1, keepdims=True)

        normals_dot_product = (normals_tgt[idx] * normals_src).sum(axis=-1)
        # Handle normals that point into wrong direction gracefully
        # (mostly due to mehtod not caring about this in generation)
        normals_dot_product = np.abs(normals_dot_product)
    else:
        normals_dot_product = np.array(
            [np.nan] * points_src.shape[0], dtype=np.float32)
    return dist, normals_dot_product

def generate_mesh_sdf(img, model, obj_path, sdf_path, iso=0.003, box_size=1.01, resolution=64):
    # create cube
    min_box = -box_size/2
    max_box = box_size/2
    x_ = np.linspace(min_box, max_box, resolution+1)
    y_ = np.linspace(min_box, max_box, resolution+1)
    z_ = np.linspace(min_box, max_box, resolution+1)

    z, y, x = np.meshgrid(z_, y_, x_, indexing='ij')
    x = np.expand_dims(x, 3)
    y = np.expand_dims(y, 3)
    z = np.expand_dims(z, 3)
    all_pts = np.concatenate((x, y, z), axis=3).astype(np.float32)
    all_pts = all_pts.reshape(1, -1, 3)

    all_pts = Variable(torch.FloatTensor(all_pts)).cuda()

    pred_sdf = model(all_pts, img)
    pred_sdf = pred_sdf.data.cpu().numpy().reshape(-1)

    # import pdb; pdb.set_trace()



    f_sdf_bin = open(sdf_path, 'wb')
    f_sdf_bin.write(struct.pack('i', -(resolution)))  # write an int
    f_sdf_bin.write(struct.pack('i', (resolution)))  # write an int
    f_sdf_bin.write(struct.pack('i', (resolution)))  # write an int

    pos = np.array([min_box, min_box, min_box, max_box, max_box, max_box])

    positions = struct.pack('d' * len(pos), *pos)
    f_sdf_bin.write(positions)
    val = struct.pack('=%sf'%pred_sdf.shape[0], *(pred_sdf))
    f_sdf_bin.write(val)
    f_sdf_bin.close()

    marching_cube_cmd = "./isosurface/computeMarchingCubes" + " " + sdf_path + " " + \
                    obj_path + " -i " + str(iso)
    os.system(marching_cube_cmd) 


def generate_mesh_mise_sdf(img, points, model, threshold=0.003, box_size=1.7, \
            resolution=64, upsampling_steps=2):
    '''
    Generates mesh for occupancy representations using MISE algorithm
    '''
    model.eval()

    resolution0 = resolution // (2**upsampling_steps)

    total_points = (resolution+1)**3
    split_size = int(np.ceil(total_points*1.0/128**3))
    mesh_extractor = MISE(
        resolution0, upsampling_steps, threshold)
    p = mesh_extractor.query()
    with torch.no_grad():
        feats = model.encoder(img)
    while p.shape[0] != 0:

        pq = p / mesh_extractor.resolution

        pq = box_size * (pq - 0.5)
        occ_pred = []
        with torch.no_grad():
            if pq.shape[0] > 128**3:

                pq = np.array_split(pq, split_size)

                for ind in range(split_size):

                    occ_pred_split = model.decoder(torch.FloatTensor(pq[ind])\
                            .cuda().unsqueeze(0), feats)
                    occ_pred.append(occ_pred_split.cpu().numpy().reshape(-1))
                occ_pred = np.concatenate(np.asarray(occ_pred),axis=0)
                values = occ_pred.reshape(-1)

            else:
                pq = torch.FloatTensor(pq).cuda().unsqueeze(0)

                occ_pred = model.decoder(pq, feats)
                values = occ_pred.squeeze(0).detach().cpu().numpy()
        values = values.astype(np.float64)
        mesh_extractor.update(p, values)

        p = mesh_extractor.query()
    value_grid = mesh_extractor.to_dense()
    mesh = extract_mesh(value_grid, feats, box_size, threshold, constant_values=1e6)
    return mesh

def clean_mesh(src_mesh, tar_mesh, dist_thresh=0.2, num_thresh=0.3):
    src_mesh_obj = pymesh.load_mesh(src_mesh)
    dis_meshes = pymesh.separate_mesh(src_mesh_obj, connectivity_type='auto')
    max_mesh_verts = 0
    dis_meshes = sorted(dis_meshes, key=lambda d_mesh: len(d_mesh.vertices))
    for dis_mesh in dis_meshes:
       if dis_mesh.vertices.shape[0] > max_mesh_verts:
           max_mesh_verts = dis_mesh.vertices.shape[0]

    collection=[]
    for i, dis_mesh in enumerate(dis_meshes):
        if dis_mesh.vertices.shape[0] > max_mesh_verts*num_thresh:
            centroid = np.mean(dis_mesh.vertices, axis=0)
            if np.sqrt(np.sum(np.square(centroid))) < dist_thresh:
                collection.append(dis_mesh)
        if len(collection) == 0 and i == len(dis_meshes)-1:
            collection = dis_meshes
    if len(collection) > 1:
        collection = sorted(collection, key=lambda d_mesh: len(d_mesh.vertices))
        # import pdb; pdb.set_trace()
        new_collection = []
        for i, dis_mesh in enumerate(collection):
            if trimesh.Trimesh(dis_mesh.vertices, dis_mesh.faces).is_volume\
                     == True:
                new_collection.append(dis_mesh)
            if len(new_collection) == 0 and i == len(collection)-1:
                new_collection = collection  
        collection = new_collection  
    tar_mesh_obj = pymesh.merge_meshes(collection)
    pymesh.save_mesh_raw(tar_mesh, tar_mesh_obj.vertices, tar_mesh_obj.faces)


def generate_mesh_mise_sdf(img, points, model, threshold=0.003, box_size=1.7, \
            resolution=64, upsampling_steps=2):
    '''
    Generates mesh for occupancy representations using MISE algorithm
    '''
    model.eval()

    resolution0 = resolution // (2**upsampling_steps)

    total_points = (resolution+1)**3
    split_size = int(np.ceil(total_points*1.0/128**3))
    mesh_extractor = MISE(
        resolution0, upsampling_steps, threshold)
    p = mesh_extractor.query()
    with torch.no_grad():
        feats = model.encoder(img)
    while p.shape[0] != 0:

        pq = p / mesh_extractor.resolution

        pq = box_size * (pq - 0.5)
        occ_pred = []
        with torch.no_grad():
            if pq.shape[0] > 128**3:

                pq = np.array_split(pq, split_size)

                for ind in range(split_size):

                    occ_pred_split = model.decoder(torch.FloatTensor(pq[ind])\
                            .cuda().unsqueeze(0), feats)
                    occ_pred.append(occ_pred_split.cpu().numpy().reshape(-1))
                occ_pred = np.concatenate(np.asarray(occ_pred),axis=0)
                values = occ_pred.reshape(-1)

            else:
                pq = torch.FloatTensor(pq).cuda().unsqueeze(0)

                occ_pred = model.decoder(pq, feats)
                values = occ_pred.squeeze(0).detach().cpu().numpy()
        values = values.astype(np.float64)
        mesh_extractor.update(p, values)

        p = mesh_extractor.query()
    value_grid = mesh_extractor.to_dense()
    mesh = extract_mesh(value_grid, feats, box_size, threshold, constant_values=1e6)
    return mesh

def writeimg(mask, pred, gt, path, cl_count=-1):
    # import pdb; pdb.set_trace()
    pred = pred.squeeze(0).cpu().numpy()
    gt = gt.squeeze(0).cpu().numpy()

    pred = pred.transpose(1,2,0)*255.
    gt = gt.transpose(1,2,0)*255.
    if len(mask) != 0:
        mask = mask.transpose(1,2,0)

        pred[mask == False] = 0. # Set background to black

    if pred.shape[-1] == 1:
        pred = np.squeeze(pred, 2)
        gt = np.squeeze(gt, 2)


    pred = Image.fromarray(np.uint8(pred))
    gt = Image.fromarray(np.uint8(gt))

    if cl_count != -1:
        pred.save(os.path.join(path, '%s-pred.png'%(str(cl_count))), "PNG")
        gt.save(os.path.join(path, '%s-gt.png'%(str(cl_count))), "PNG")
    else:
        pred.save(os.path.join(path, 'pred.png'), "PNG")
        gt.save(os.path.join(path, 'gt.png'), "PNG")

def compute_iou_seg(pred, gt):
    ''' Computes the Intersection over Union (IoU) value for two sets of
    occupancy values.

    Args:
        occ1 (tensor): first set of occupancy values
        occ2 (tensor): second set of occupancy values
    '''
    occ1 = pred.cpu().numpy()
    occ2 = gt.cpu().numpy()

    occ1 = (occ1 >= 0.5).reshape(len(occ1),-1)
    occ2 = (occ2 >= 0.5).reshape(len(occ2),-1)

    # Compute IOU
    area_union = (occ1 | occ2).astype(np.float32).sum(axis=-1)
    if (area_union == 0).any():
        # import pdb; pdb.set_trace()
        return 0.

    area_intersect = (occ1 & occ2).astype(np.float32).sum(axis=-1)

    iou = (area_intersect / area_union)

    return iou










