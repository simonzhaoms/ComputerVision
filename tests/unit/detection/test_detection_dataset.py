# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

import numpy as np
import pytest
import torch
from pathlib import Path
from torch import Tensor
from PIL import Image
from typing import Tuple, List

from utils_cv.detection.dataset import (
    get_transform,
    parse_pascal_voc_anno,
    DetectionDataset,
)
from utils_cv.detection.bbox import AnnotationBbox, _Bbox
from utils_cv.detection.keypoint import CartonKeypoints


@pytest.fixture(scope="session")
def basic_im(od_cup_path) -> Tuple[Image.Image, dict]:
    """ returns an Image, Target tuple. """
    im = Image.open(od_cup_path).convert("RGB")

    boxes = torch.as_tensor([[61, 59, 273, 244]], dtype=torch.float32)
    labels = torch.as_tensor([[0]], dtype=torch.int64)
    masks = np.zeros((500, 500), dtype=np.bool)
    masks[100:200, 100:200] = True
    masks = torch.as_tensor(masks, dtype=torch.uint8)

    target = {
        "boxes": boxes,
        "labels": labels,
        "image_id": None,
        "area": None,
        "iscrowd": False,
        "masks": masks,
    }

    return im, target


@pytest.fixture(scope="session")
def od_sample_bboxes() -> List[_Bbox]:
    """ Returns the true bboxes from the `od_sample_im_anno` fixture. """
    return [_Bbox(left=100, top=173, right=233, bottom=521)]


@pytest.fixture(scope="session")
def od_sample_carton_keypoints() -> np.array:
    """ Returns the true keypoints from the `od_sample_im_anno_keypoint`
    fixture. """
    return np.array([[
        [175, 204, 2],
        [114, 176, 2],
        [210, 179, 2],
        [111, 203, 2],
        [215, 205, 2],
        [121, 245, 2],
        [233, 241, 2],
        [103, 228, 2],
        [0, 0, 0],
        [138, 519, 2],
        [230, 496, 2],
        [117, 468, 2],
        [0, 0, 0],
    ]])


@pytest.fixture(scope="session")
def basic_detection_dataset(tiny_od_data_path) -> DetectionDataset:
    return DetectionDataset(tiny_od_data_path)


def test_basic_im(basic_im):
    im, target = basic_im
    assert type(im) == Image.Image
    assert type(target) == dict


def test_get_transform(basic_im):
    """ assert that the basic transformation of converting to tensor is
    achieved. """
    im, target = basic_im
    assert type(im) == Image.Image
    tfms_im, tfms_target = get_transform(train=True)(im, target)
    assert type(tfms_im) == Tensor
    tfms_im, tfms_target = get_transform(train=False)(im, target)
    assert type(tfms_im) == Tensor


def test_parse_pascal_voc(
    od_sample_im_anno,
    od_sample_bboxes,
    od_sample_im_anno_keypoint,
    od_sample_carton_keypoints,
):
    """ test that 'parse_pascal_voc' can parse the 'od_sample_im_anno' and
    'od_sample_im_anno_keypoint' correctly. """
    anno_path, im_path = od_sample_im_anno
    anno_bboxes, im_path, _ = parse_pascal_voc_anno(anno_path)
    assert type(anno_bboxes[0]) == AnnotationBbox
    assert anno_bboxes[0].left == od_sample_bboxes[0].left
    assert anno_bboxes[0].right == od_sample_bboxes[0].right
    assert anno_bboxes[0].top == od_sample_bboxes[0].top
    assert anno_bboxes[0].bottom == od_sample_bboxes[0].bottom

    anno_path, _ = od_sample_im_anno_keypoint
    _, _, keypoints = parse_pascal_voc_anno(anno_path)
    np.all(keypoints == od_sample_carton_keypoints)


def validate_detection_dataset(data: DetectionDataset, labels: List[str]):
    data_length = 39
    if len(data.mask_paths) > 0:
        data_length = 31
    elif len(data.keypoints) > 0:
        data_length = 20   # only 20 of 30 the tiny keypoints images contain carton
    assert len(data) == data_length

    assert type(data) == DetectionDataset

    assert len(data.labels) == 4 if len(data.keypoints) == 0 else 1
    for label in data.labels:
        assert label in labels

    if data.mask_paths:
        assert len(data.mask_paths) == len(data.im_paths)

    if len(data.keypoints) > 0:
        assert len(data.keypoints) == len(data.im_paths)


def test_detection_dataset_init_basic(
    tiny_od_data_path,
    od_data_path_labels,
    tiny_od_mask_data_path,
    tiny_od_keypoint_data_path,
):
    """ Tests that initialization of the Detection Dataset works. """
    data = DetectionDataset(tiny_od_data_path)
    validate_detection_dataset(data, od_data_path_labels)
    assert len(data.test_ds) == 19
    assert len(data.train_ds) == 20

    # test random seed
    data = DetectionDataset(tiny_od_data_path, seed=9)
    data2 = DetectionDataset(tiny_od_data_path, seed=9)
    assert data.train_dl.dataset.indices == data2.train_dl.dataset.indices
    assert data.test_dl.dataset.indices == data2.test_dl.dataset.indices

    # test mask data
    data = DetectionDataset(
        tiny_od_mask_data_path,
        mask_dir="segmentation-masks"
    )
    validate_detection_dataset(data, od_data_path_labels)
    assert len(data.test_ds) == 15
    assert len(data.train_ds) == 16

    # test keypoint data
    data = DetectionDataset(
        tiny_od_keypoint_data_path,
        keypoint_meta=CartonKeypoints.to_dict(),
    )
    validate_detection_dataset(data, od_data_path_labels)
    assert len(data.test_ds) == 10
    assert len(data.train_ds) == 10


def test_detection_dataset_init_train_pct(
    tiny_od_data_path,
    od_data_path_labels,
    tiny_od_mask_data_path,
    tiny_od_keypoint_data_path,
):
    """ Tests that initialization with train_pct."""
    data = DetectionDataset(tiny_od_data_path, train_pct=0.75)
    validate_detection_dataset(data, od_data_path_labels)
    assert len(data.test_ds) == 9
    assert len(data.train_ds) == 30

    # test mask data
    data = DetectionDataset(
        tiny_od_mask_data_path,
        train_pct=0.75,
        mask_dir="segmentation-masks"
    )
    validate_detection_dataset(data, od_data_path_labels)
    assert len(data.test_ds) == 7
    assert len(data.train_ds) == 24

    # test keypoint data
    data = DetectionDataset(
        tiny_od_keypoint_data_path,
        train_pct=0.75,
        keypoint_meta=CartonKeypoints.to_dict(),
    )
    validate_detection_dataset(data, od_data_path_labels)
    assert len(data.test_ds) == 5
    assert len(data.train_ds) == 15


def test_detection_dataset_show_ims(
    basic_detection_dataset,
    od_detection_mask_dataset,
    od_detection_keypoint_dataset,
):
    # simply test that this is error free for now
    basic_detection_dataset.show_ims()
    od_detection_mask_dataset.show_ims()
    od_detection_keypoint_dataset.show_ims()


def test_detection_dataset_show_im_transformations(
    basic_detection_dataset,
    od_detection_mask_dataset,
    od_detection_keypoint_dataset,
):
    # simply test that this is error free for now
    basic_detection_dataset.show_im_transformations()
    od_detection_mask_dataset.show_im_transformations()
    od_detection_keypoint_dataset.show_im_transformations()


def test_detection_dataset_init_anno_im_dirs(
    func_tiny_od_data_path, od_data_path_labels
):
    """ Tests that initialization with renamed anno/im dirs.
    NOTE: this test doesn't use the normal tiny_od_data_path fixture since it
    modifies the files in it. instead it uses the function level fixture.
    """
    data_path = Path(func_tiny_od_data_path)
    new_anno_dir_name = "bounding_boxes"
    new_im_dir_name = "photos"
    anno_dir = data_path / "annotations"
    anno_dir.rename(data_path / new_anno_dir_name)
    im_dir = data_path / "images"
    im_dir.rename(data_path / new_im_dir_name)
    data = DetectionDataset(
        str(data_path), anno_dir=new_anno_dir_name, im_dir=new_im_dir_name
    )
    validate_detection_dataset(data, od_data_path_labels)
