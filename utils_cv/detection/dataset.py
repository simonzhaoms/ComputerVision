# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

import os
import math
import numpy as np
from pathlib import Path
import random
from typing import Callable, List, Tuple, Union

import torch
from torch.utils.data import Dataset, Subset, DataLoader
import xml.etree.ElementTree as ET
from PIL import Image
from pycocotools.coco import COCO

from .plot import (
    display_bboxes,
    display_bbox_mask_keypoint,
    plot_grid,
)
from .bbox import AnnotationBbox
from .data import coco_labels
from .mask import binarise_mask
from .references.utils import collate_fn
from .references.transforms import Compose, RandomHorizontalFlip, ToTensor
from ..common.gpu import db_num_workers

Trans = Callable[[object, dict], Tuple[object, dict]]


def get_transform(train: bool) -> Trans:
    """ Gets basic the transformations to apply to images.

    Source:
    https://pytorch.org/tutorials/intermediate/torchvision_tutorial.html#writing-a-custom-dataset-for-pennfudan

    Args:
        train: whether or not we are getting transformations for the training
        set.

    Returns:
        A list of transforms to apply.
    """
    transforms = [ToTensor()]
    if train:
        transforms.append(RandomHorizontalFlip(0.5))
        # TODO we can add more 'default' transformations here
    return Compose(transforms)


def parse_pascal_voc_anno(
    anno_path: str, labels: List[str] = None
) -> Tuple[List[AnnotationBbox], Union[str, Path]]:
    """ Extract the annotations and image path from labelling in Pascal VOC
    format.

    Args:
        anno_path: the path to the annotation xml file
        labels: list of all possible labels, used to compute label index for
                each label name

    Return
        A tuple of annotations and the image path
    """

    anno_bboxes = []
    tree = ET.parse(anno_path)
    root = tree.getroot()

    # get image path from annotation. Note that the path field might not be
    # set.
    anno_dir = os.path.dirname(anno_path)
    if root.find("path"):
        im_path = os.path.realpath(
            os.path.join(anno_dir, root.find("path").text)
        )
    else:
        im_path = os.path.realpath(
            os.path.join(anno_dir, root.find("filename").text)
        )

    # extract bounding boxes and classification
    objs = root.findall("object")
    for obj in objs:
        label = obj.find("name").text
        bnd_box = obj.find("bndbox")
        left = int(bnd_box.find('xmin').text)
        top = int(bnd_box.find('ymin').text)
        right = int(bnd_box.find('xmax').text)
        bottom = int(bnd_box.find('ymax').text)

        # Set mapping of label name to label index
        if labels is None:
            label_idx = None
        else:
            label_idx = labels.index(label)

        anno_bbox = AnnotationBbox.from_array(
            [left, top, right, bottom],
            label_name=label,
            label_idx=label_idx,
            im_path=im_path,
        )
        assert anno_bbox.is_valid()
        anno_bboxes.append(anno_bbox)

    return anno_bboxes, im_path


class DetectionDataset(Dataset):
    """ An object detection dataset.

    The dunder methods __init__, __getitem__, and __len__ were inspired from
    code found here:
    https://pytorch.org/tutorials/intermediate/torchvision_tutorial.html#writing-a-custom-dataset-for-pennfudan
    """

    def __init__(
        self,
        root: Union[str, Path],
        batch_size: int = 2,
        transforms: Union[Trans, Tuple[Trans, Trans]] = (
                get_transform(train=True),
                get_transform(train=False)
        ),
        train_pct: float = 0.5,
        anno_dir: str = "annotations",
        im_dir: str = "images",
        mask_dir: str = None,
        anno_file: str = None,
        seed: int = None,
    ):
        """ initialize dataset

        This class assumes that the data is formatted in two folders:
            - annotation folder which contains the Pascal VOC formatted
              annotations
            - image folder which contains the images

        Args:
            root: the root path of the dataset containing the image and
                  annotation folders
            batch_size: batch size for dataloaders
            transforms: the transformations to apply
            train_pct: the ratio of training to testing data
            anno_dir: the name of the annotation subfolder under the root
                      directory
            im_dir: the name of the image subfolder under the root directory.
                    If set to 'None' then infers image location from annotation
                    .xml files
        """

        self.root = Path(root)
        # TODO think about how transforms are working...
        if transforms and len(transforms) == 1:
            self.transforms = (transforms, ) * 2
        self.transforms = transforms
        self.im_dir = im_dir
        self.anno_dir = anno_dir
        self.mask_dir = mask_dir
        self.anno_file = anno_file
        self.batch_size = batch_size
        self.train_pct = train_pct
        self.seed = seed

        # read annotations
        self._read_annos()

        self._get_dataloader(train_pct)

    def _get_dataloader(self, train_pct):
        # create training and validation datasets
        train_ds, test_ds = self.split_train_test(
            train_pct=train_pct
        )

        # create training and validation data loaders
        self.train_dl = DataLoader(
            train_ds,
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=db_num_workers(),
            collate_fn=collate_fn,
        )
        self.test_dl = DataLoader(
            test_ds,
            batch_size=self.batch_size,
            shuffle=False,
            num_workers=db_num_workers(),
            collate_fn=collate_fn,
        )

    def _read_annos(self) -> None:
        """ Parses all Pascal VOC formatted annotation files to extract all
        possible labels. """

        # For COCO data: A single JSON annotation file should be provided.
        #
        # For PASCAL VOC dataset:
        # All annotation files are assumed to be in the anno_dir directory.
        # If im_dir is provided then find all images in that directory, and
        # it's assumed that the annotation filenames end with .xml.
        # If im_dir is not provided, then the image paths are read from inside
        # the .xml annotations.
        if self.anno_file:
            self.coco = COCO(self.anno_file)
            self.SIZE = 20
            img_ids = list(self.coco.imgs.keys())[:self.SIZE]
        elif self.im_dir is None:
            anno_filenames = sorted(os.listdir(self.root / self.anno_dir))
        else:
            im_filenames = sorted(os.listdir(self.root / self.im_dir))
            im_paths = [
                os.path.join(self.root / self.im_dir, s) for s in im_filenames
            ]
            anno_filenames = [
                os.path.splitext(s)[0] + ".xml" for s in im_filenames
            ]

        # Parse all annotations
        if self.anno_file:
            self.im_paths = [
                Path(self.im_dir) / self.coco.imgs[i]["file_name"]
                for i in img_ids
            ]
            self.annos_list = [self.coco.imgToAnns[i] for i in img_ids]
            # ignore images without annotated objects
            valid_idxes = [
                i for i, annos in enumerate(self.annos_list) if len(annos) != 0
            ]
            self.im_paths = [self.im_paths[i] for i in valid_idxes]
            self.annos_list = [self.annos_list[i] for i in valid_idxes]
            self.anno_bboxes = [
                [
                    AnnotationBbox.from_array_xywh(
                        anno["bbox"],
                        label_idx=None,
                        label_name=coco_labels()[anno["category_id"]]
                    ) for anno in annos
                ] for annos in self.annos_list
            ]
        else:
            self.im_paths = []
            self.anno_paths = []
            self.anno_bboxes = []
            self.mask_paths = []
            for anno_idx, anno_filename in enumerate(anno_filenames):
                anno_path = self.root / self.anno_dir / str(anno_filename)
                assert os.path.exists(
                    anno_path
                ), f"Cannot find annotation file: {anno_path}"
                anno_bboxes, im_path = parse_pascal_voc_anno(anno_path)

                # TODO For now, ignore all images without a single bounding box
                #      in it, otherwise throws error during training.
                if len(anno_bboxes) == 0:
                    continue

                if self.im_dir is None:
                    self.im_paths.append(im_path)
                else:
                    self.im_paths.append(im_paths[anno_idx])

                if self.mask_dir:
                    mask_name = os.path.basename(self.im_paths[-1])
                    mask_name = mask_name[:mask_name.rindex('.')] + ".png"
                    mask_name = self.root / self.mask_dir / mask_name
                    # For mask prediction, if no mask provided, ignore the image
                    if not mask_name.exists():
                        del self.im_paths[-1]
                        continue

                    self.mask_paths.append(mask_name)

                self.anno_paths.append(anno_path)
                self.anno_bboxes.append(anno_bboxes)

            assert len(self.im_paths) == len(self.anno_paths)

        # Get list of all labels
        labels = []
        for anno_bboxes in self.anno_bboxes:
            for anno_bbox in anno_bboxes:
                labels.append(anno_bbox.label_name)
        self.labels = list(set(labels))

        # Set for each bounding box label name also what its integer
        # representation is
        for anno_bboxes in self.anno_bboxes:
            for anno_bbox in anno_bboxes:
                anno_bbox.label_idx = (
                    self.labels.index(anno_bbox.label_name) + 1
                )

    def split_train_test(
        self, train_pct: float = 0.8
    ) -> Tuple[Dataset, Dataset]:
        """ Split this dataset into a training and testing set

        Args:
            train_pct: the ratio of images to use for training vs

        Return
            A training and testing dataset in that order
        """
        # TODO Is it possible to make these lines in split_train_test() a bit
        #      more intuitive?

        test_num = math.floor(len(self) * (1 - train_pct))
        if self.seed:
            torch.manual_seed(self.seed)
        indices = torch.randperm(len(self)).tolist()

        train_idx = indices[test_num:]
        test_idx = indices[: test_num + 1]

        # indicate whether the data are for training or testing
        self.is_test = np.zeros((len(self),), dtype=np.bool)
        self.is_test[test_idx] = True

        train = Subset(self, train_idx)
        test = Subset(self, test_idx)

        return train, test

    def _get_transforms(self, idx):
        """ Return the corresponding transforms for training and testing data. """
        return self.transforms[self.is_test[idx]]

    def show_ims(self, rows: int = 1, cols: int = 3, seed: int = None) -> None:
        """ Show a set of images.

        Args:
            rows: the number of rows images to display
            cols: cols to display, NOTE: use 3 for best looking grid
            seed: random seed for selecting images

        Returns None but displays a grid of annotated images.
        """
        if seed or self.seed:
            random.seed(seed or self.seed)

        plot_func = (
            display_bbox_mask_keypoint
            if self.mask_paths or self.anno_file
            else display_bboxes
        )
        plot_grid(plot_func, self._get_random_anno, rows=rows, cols=cols)

    def _get_binary_masks(self, idx: int) -> Union[np.ndarray, None]:
        binary_masks = None
        if self.anno_file and [a for a in self.annos_list[idx] if "segmentation" in a]:
            binary_masks = np.array([
                self.coco.annToMask(a) for a in self.annos_list[idx]
                if "segmentation" in a
            ])
        elif self.mask_paths:
            binary_masks = binarise_mask(Image.open(self.mask_paths[idx]))

        return binary_masks

    def _get_keypoints(self, idx: int) -> Union[np.ndarray, None]:
        keypoints = None
        if self.anno_file and [a for a in self.annos_list[idx] if "keypoints" in a]:
            keypoints = np.array([a["keypoints"] for a in self.annos_list[idx] if "keypoints" in a])
            keypoints = (
                keypoints.reshape((len(keypoints), -1, 3))
                if keypoints.size else None
            )

        return keypoints

    def _get_random_anno(self) -> Tuple:
        """ Get random annotation and corresponding image

        Returns a list of annotations and the image path
        """
        idx = random.randrange(len(self.im_paths))

        # get mask if any
        mask = self._get_binary_masks(idx)

        # get keypoints if any
        keypoints = self._get_keypoints(idx)

        if mask is not None or keypoints is not None:
            return self.anno_bboxes[idx], self.im_paths[idx], mask, keypoints
        else:
            return self.anno_bboxes[idx], self.im_paths[idx]

    def __getitem__(self, idx):
        """ Make iterable. """
        # get box/labels from annotations
        anno_bboxes = self.anno_bboxes[idx]
        boxes = [anno_bbox.rect() for anno_bbox in anno_bboxes]
        labels = [anno_bbox.label_idx for anno_bbox in anno_bboxes]

        # get area for evaluation with the COCO metric, to separate the
        # metric scores between small, medium and large boxes.
        area = [b.surface_area() for b in anno_bboxes]

        # setup target dic
        target = {
            "boxes": torch.as_tensor(boxes, dtype=torch.float32),
            "labels": torch.as_tensor(labels, dtype=torch.int64),
            # unique id
            "image_id": torch.tensor([idx]),
            "area": torch.as_tensor(area, dtype=torch.float32),
            # suppose all instances are not crowd (torchvision specific)
            "iscrowd": torch.zeros((len(boxes),), dtype=torch.int64),
        }

        # get masks
        binary_masks = self._get_binary_masks(idx)
        if binary_masks is not None:
            target["masks"] = torch.as_tensor(binary_masks, dtype=torch.uint8)

        # get keypoints
        keypoints = self._get_keypoints(idx)
        if keypoints is not None:
            target["keypoints"] = torch.as_tensor(
                keypoints,
                dtype=torch.float32,
            )

        # get image
        im = Image.open(self.im_paths[idx]).convert("RGB")

        # and apply transforms if any
        if self.transforms:
            im, target = self._get_transforms(idx)(im, target)

        return im, target

    def __len__(self):
        return len(self.im_paths)
