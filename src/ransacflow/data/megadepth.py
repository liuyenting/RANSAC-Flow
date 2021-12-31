from collections import defaultdict
import io
import logging
import pickle
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from zipfile import ZipFile

import numpy as np
import pandas as pd
import pytorch_lightning as pl
import torch
from torchvision.transforms import Compose

from . import transform
from .dataset import ZippedImageFolder

__all__ = ["MegaDepthDataModule"]

logger = logging.getLogger("ransacflow.data.megadepth")


class MegaDepthTrainingDataset(ZippedImageFolder):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rng = np.random.default_rng()

        if self.target_transform is not None:
            logger.warning("target_transform is not used")

    @staticmethod
    def make_dataset(
        zip: Tuple[ZipFile, Path],
        class_to_idx: Dict[str, int],
        extensions: Optional[Tuple[str, ...]] = None,
        is_valid_file: Optional[Callable[[str], bool]] = None,
    ) -> List[Tuple[str, int]]:
        items = super(MegaDepthTrainingDataset, MegaDepthTrainingDataset).make_dataset(
            zip, class_to_idx, extensions, is_valid_file
        )

        # rebuild dictionary, and do sanity check
        instances = defaultdict(list)
        for file, target_class in items:
            instances[target_class].append(file)
        for target_class, files in instances.items():
            assert len(files) == 3, f"'{target_class}' does not have 3 images"

        return list((v, k) for k, v in instances.items())

    def __getitem__(self, index: int):
        # we need 2 images, randomly choose from [i+0]-[i+2] (3 images)
        files, _ = self.samples[index]
        offsets = self._rng.choice(3, size=2, replace=False)

        # we cannot use parent __getitem__ since transformations will be off
        image_pair = []
        for offset in offsets:
            file = files[offset]

            stream = self._handle.open(file)
            image = self.loader(stream)

            image_pair.append(image)
        image_pair = tuple(image_pair)

        assert (
            image_pair[0].shape == image_pair[1].shape
        ), f"index {index}, image pair has different dimensions"

        if self.transform is not None:
            image_pair = self.transform(image_pair)

        return image_pair

    def __len__(self) -> int:
        return len(self.classes)


class MegaDepthValidationDataset(ZippedImageFolder):
    def __init__(self, root: Path, directory: Optional[Path] = "/", *args, **kwargs):
        # pass images directory to super
        directory = Path(directory)
        super().__init__(root, directory / "images", *args, **kwargs)

        if self.target_transform is not None:
            logger.warning("target_transform is not used")

    def find_classes(self, directory: Path) -> Tuple[List[str], Dict[str, int]]:
        """
        Find class folders in a dataset.

        This method follows the original implementation, but operates inside a ZIP file.

        Args:
            directory (Path): Directory path inside the ZIP file.

        Raises:
            FileNotFounderror: If `directory` has no class folders.

        Returns:
            (Tuple[List[str], Dict[str, int]]): List of all classes, and a dictionary
                mapping each class to an index.
        """
        # normalize directory with trailing slash
        #   https://bugs.python.org/issue21039
        directory /= ""
        if not directory.exists():
            raise ValueError(f"unable to locate '{directory}' in the ZIP file")

        # after VisionDataset.__init__, root holds the zipfile.Path to images
        # but, our match list is one folder upward
        path = directory.parent / "matches.csv"
        fp = io.BytesIO(path.read_bytes())
        matches = pd.read_csv(fp)

        # matches[scene] contains the class as integer, convert to list of str
        #   https://pandas.pydata.org/pandas-docs/stable/user_guide/text.html
        classes = matches["scene"].astype("string").tolist()
        classes = list(set(classes))
        for cls_name in classes:
            class_path = directory / cls_name
            if not class_path.exists():
                raise FileNotFoundError(f"could not find '{cls_name}'")

        class_to_idx = {cls_name: i for i, cls_name in enumerate(classes)}
        return classes, class_to_idx

    @staticmethod
    def make_dataset(
        directory: Path,
        class_to_idx: Dict[str, int],
        extensions: Optional[Tuple[str, ...]] = None,
        is_valid_file: Optional[Callable[[str], bool]] = None,
    ) -> List[Tuple[str, int]]:
        if class_to_idx is None:
            # we explicitly want to use `find_classes` method
            raise ValueError("'class_to_idx' parameter cannot be None")

        if not ((extensions is None) ^ (is_valid_file is None)):
            raise ValueError(
                "both 'extensions' and 'is_valid_file' cannot be None or not None at the same time"
            )
        if extensions is not None:
            # x should be a Path-like object
            logger.warning("file integrity is not explicitly tested")

        # normalize directory with trailing slash
        #   https://bugs.python.org/issue21039
        directory /= ""

        # we don't want to work with DataFrame, convert them all to ndarray
        path = directory.parent / "matches.csv"
        fp = io.BytesIO(path.read_bytes())
        matches = pd.read_csv(fp)
        # convert some units first
        matches["scene"] = matches["scene"].astype("string")

        # load ground truth transformation matrix
        path = directory.parent / "affine.pkl"
        with path.open("r") as fp:
            # it was stored as a dictionary
            affine_mats = pickle.load(fp)

        instances = []
        for (_, row), affine_mat in zip(matches.iterrows(), affine_mats.values()):
            # class name
            cls_name = row["scene"]
            class_index = class_to_idx[cls_name]

            # zipfile.Path to image
            src_path, tgt_path = row["source_image"], row["target_image"]
            src_path = directory / cls_name / src_path
            tgt_path = directory / cls_name / tgt_path

            # load and compact feature coordinates
            src_feat_x = np.fromstring(row["XA"], dtype=np.float32, sep=";")
            src_feat_y = np.fromstring(row["YA"], dtype=np.float32, sep=";")
            src_feat = np.stack([src_feat_x, src_feat_y], axis=-1)
            tgt_feat_x = np.fromstring(row["XB"], dtype=np.float32, sep=";")
            tgt_feat_y = np.fromstring(row["YB"], dtype=np.float32, sep=";")
            tgt_feat = np.stack([tgt_feat_x, tgt_feat_y], axis=-1)

            # NOTE
            # ground truth affine transformation matrix is stored directly

            item = (src_path, src_feat), (tgt_path, tgt_feat), affine_mat, class_index
            instances.append(item)

        return instances

    def __getitem__(self, index: int):
        source, target, affine_mat, _ = self.samples[index]
        src_path, src_feat = source
        tgt_path, tgt_feat = target

        # load source and target images
        fp = io.BytesIO(src_path.read_bytes())
        src_image = self.loader(fp)
        fp = io.BytesIO(tgt_path.read_bytes())
        tgt_image = self.loader(fp)

        # image pair can have different size, but feature points must match
        # this project does not take occlusion in to consideration
        assert len(src_feat) == len(tgt_feat), f"index {index}, missing feature points"

        # pack them up
        source = src_image, src_feat
        target = tgt_image, tgt_feat
        item = source, target, affine_mat

        if self.transform is not None:
            item = self.transform(item)

        return item


class MegaDepthDataModule(pl.LightningDataModule):
    """[summary]

    Args:
        path (Path): Path to the ZIP file.
        train_image_size (int or tuple of int, optional): Crop input image to this size.
        train_batch_size (int, optional): Samples per batch to load during training.
        val_image_size (int, optional): Minimum image size during validation.
        num_workers (int, optional): How many subprocesses to use for data loading.
    """

    def __init__(
        self,
        path: Path,
        train_image_size: Union[int, tuple] = 224,
        train_batch_size: int = 16,
        val_image_size: Union[int, tuple] = 480,
        num_workers: int = 2,
    ):
        super().__init__()

        self.path = path

        self.train_image_size = train_image_size
        self.train_batch_size = train_batch_size

        self.val_image_size = val_image_size

        self.num_workers = num_workers

    def setup(self, stage: Optional[str] = None):
        train_transforms = Compose(
            [
                transform.ToTensorImagePair(),
                transform.EnsureRGBImagePair(),
                transform.RandomCropImagePair(self.train_image_size),
                transform.RandomHorizontalFlipImagePair(),
            ]
        )
        self.megadepth_train = MegaDepthTrainingDataset(
            self.path, directory="train", transform=train_transforms
        )

        val_transforms = Compose(
            [
                transform.ToTensorValidationPair(),
                transform.EnsureRGBValidationPair(),
                transform.ResizeValidationImageFeaturesPair(
                    min_size=self.val_image_size
                ),
            ]
        )
        self.megadepth_val = MegaDepthValidationDataset(
            self.path, directory="validate", transform=val_transforms,
        )

    def teardown(self, stage: Optional[str] = None):
        # FIXME these datasets are zipped folder, close them for safety
        pass

    def train_dataloader(self):
        megadepth_train = torch.utils.data.DataLoader(
            self.megadepth_train,
            batch_size=self.train_batch_size,
            shuffle=True,
            drop_last=True,
            num_workers=self.num_workers,
        )
        return megadepth_train

    def val_dataloader(self):
        # NOTE most torch function has N dimension, so we keep batch_size=1 instead of
        # disable automatic batching mechanism
        megadepth_val = torch.utils.data.DataLoader(
            self.megadepth_val,
            batch_size=1,
            shuffle=False,
            num_workers=self.num_workers,
        )
        return megadepth_val
