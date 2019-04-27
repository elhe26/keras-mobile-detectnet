import numpy as np
import os
import plac

import cv2
from imgaug import augmenters as iaa

import imgaug as ia

import tensorflow.keras as keras
from tensorflow.keras.optimizers import SGD
from tensorflow.keras.callbacks import ModelCheckpoint
from tensorflow.keras.utils import Sequence

from model import MobileDetectNetModel

from sgdr import SGDRScheduler


class MobileDetectNetSequence(Sequence):
    def __init__(self,
                 path: str,
                 model: str = None,
                 stage: str = "train",
                 batch_size: int = 12,
                 resize_width: int = 224,
                 resize_height: int = 224,
                 coverage_width: int = 7,
                 coverage_height: int = 7,
                 bboxes_width: int = 7,
                 bboxes_height: int = 7
                 ):

        self.model = model

        self.images = []
        self.images_filenames = []
        self.labels = []

        for r, d, f in os.walk(os.path.join(path, "images")):
            for file in f:
                self.images.append(os.path.join(r, file))
                self.labels.append(os.path.join(path, "labels", (file.split(".")[0] + ".txt")))

        self.batch_size = batch_size
        self.resize_width = resize_width
        self.resize_height = resize_height
        self.coverage_width = coverage_width
        self.coverage_height = coverage_height
        self.bboxes_width = bboxes_width
        self.bboxes_height = bboxes_height

        self.seq = MobileDetectNetSequence.create_augmenter(stage)

    def __len__(self):
        # TODO: Do stuff with "remainder" training data
        return int(np.floor(len(self.images) / float(self.batch_size)))

    def __getitem__(self, idx):

        input_image = np.zeros((self.batch_size, self.resize_height, self.resize_width, 3))
        output_coverage_map = np.zeros((self.batch_size, self.coverage_height, self.coverage_width))

        # We need 4 fields for bboxes, but we temporarily use 5 to keep track of which bbox has a better claim
        output_bboxes = np.zeros((self.batch_size, self.bboxes_height, self.bboxes_width, 5))
        output_bboxes_center = np.zeros((self.batch_size, self.bboxes_height, self.bboxes_width, 4))

        for i in range(0, self.batch_size):

            seq_det = self.seq.to_deterministic()

            image = cv2.imread(self.images[idx * self.batch_size + i])
            old_shape = image.shape
            image = cv2.resize(image, (self.resize_height, self.resize_width))

            bboxes, segmap = MobileDetectNetSequence.load_kitti_label(image,
                                                                      scale=(image.shape[0] / old_shape[0],
                                                                             image.shape[1] / old_shape[1]),
                                                                      label=self.labels[idx * self.batch_size + i])

            image_aug = seq_det.augment_image(image)
            bboxes_aug = seq_det.augment_bounding_boxes(bboxes).remove_out_of_image().clip_out_of_image()

            segmap_aug = seq_det.augment_segmentation_maps(segmap)

            output_segmap = segmap_aug.arr.astype(np.float32)
            output_segmap = cv2.resize(output_segmap, (self.coverage_height, self.coverage_width),
                                       interpolation=cv2.INTER_AREA).astype(np.float32)[:, :, 1]

            # Work on building a batch
            input_image[i] = (image_aug.astype(np.float32) / 127.5) - 1.  # "tf" style normalization
            output_coverage_map[i] = output_segmap

            for bbox in bboxes_aug.bounding_boxes:

                # Put a bbox in each title of its coverage map
                for y in range(0, self.coverage_height):
                    for x in range(0, self.coverage_width):

                        bx1 = (self.coverage_width * bbox.x1 / self.resize_width)
                        bx2 = (self.coverage_width * bbox.x2 / self.resize_width)

                        by1 = (self.coverage_height * bbox.y1 / self.resize_height)
                        by2 = (self.coverage_height * bbox.y2 / self.resize_height)

                        if np.floor(bx1) <= x <= np.ceil(bx2) and np.floor(by1) <= y <= np.ceil(by2):

                            x_in = max(0, min(x + 1, bx2) - max(x, bx1))
                            y_in = max(0, min(y + 1, by2) - max(y, by1))
                            area_in = x_in * y_in

                            # Prioritize the most dominant box in the coverage tile
                            if 0.75 <= area_in > output_bboxes[i, y, x, 4]:
                                output_bboxes[i, int(y), int(x), 0] = bbox.x1 / self.resize_width
                                output_bboxes[i, int(y), int(x), 1] = bbox.y1 / self.resize_height
                                output_bboxes[i, int(y), int(x), 2] = bbox.x2 / self.resize_width
                                output_bboxes[i, int(y), int(x), 3] = bbox.y2 / self.resize_height
                                output_bboxes[i, int(y), int(x), 4] = area_in

                bbox_center_x = int(self.coverage_width * ((bbox.x2 + bbox.x1) / 2) / self.resize_width)
                bbox_center_y = int(self.coverage_height * ((bbox.y2 + bbox.y1) / 2) / self.resize_height)

                output_bboxes_center[i, bbox_center_y, bbox_center_x, 0] = bbox.x1 / self.resize_width
                output_bboxes_center[i, bbox_center_y, bbox_center_x, 1] = bbox.y1 / self.resize_height
                output_bboxes_center[i, bbox_center_y, bbox_center_x, 2] = bbox.x2 / self.resize_width
                output_bboxes_center[i, bbox_center_y, bbox_center_x, 3] = bbox.y2 / self.resize_height

        # Remove the "claim" bbox field so it matches the network output
        output_bboxes = output_bboxes[:, :, :, 0:4]

        if self.model is None or self.model == "complete":
            return input_image, [
                output_coverage_map.reshape((self.batch_size, self.coverage_height, self.coverage_width, 1)),
                output_bboxes, output_bboxes_center]
        elif self.model == "coverage":
            return input_image, [
                output_coverage_map.reshape((self.batch_size, self.coverage_height, self.coverage_width, 1))]
        elif self.model == "region":
            return output_coverage_map.reshape(
                (self.batch_size, self.coverage_height, self.coverage_width, 1)), output_bboxes
        elif self.model == "pooling":
            return output_bboxes, output_bboxes_center

    @staticmethod
    # KITTI Format Labels
    def load_kitti_label(image: np.ndarray, scale, label: str):

        label = open(label, 'r').read()

        bboxes = []

        segmap = np.zeros((image.shape[0], image.shape[1], 3), dtype=np.uint8)

        for row in label.split('\n'):
            fields = row.split(' ')

            bbox_class = fields[0]

            # TODO: Can we use this information to generate more accurate segmentation maps or bboxes?
            bbox_truncated = float(fields[1])
            bbox_occluded = int(fields[2])
            bbox_alpha = float(fields[3])

            bbox_x1 = float(fields[4]) * scale[1]
            bbox_y1 = float(fields[5]) * scale[0]
            bbox_x2 = float(fields[6]) * scale[1]
            bbox_y2 = float(fields[7]) * scale[0]

            polygon = ia.Polygon(
                np.float32([[bbox_x1, bbox_y1], [bbox_x1, bbox_y2], [bbox_x2, bbox_y2], [bbox_x2, bbox_y1]]))
            segmap = polygon.draw_on_image(segmap, alpha=1.0, alpha_perimeter=0.0)

            bbox = ia.BoundingBox(bbox_x1, bbox_y1, bbox_x2, bbox_y2, bbox_class)
            bboxes.append(bbox)

        segmap = np.argmax(segmap, axis=2)

        bboi = ia.BoundingBoxesOnImage(bboxes, shape=image.shape)
        smoi = ia.SegmentationMapOnImage(segmap, shape=image.shape, nb_classes=2)

        return bboi, smoi

    @staticmethod
    def create_augmenter(stage: str = "train"):
        if stage == "train":
            return iaa.Sequential([
                iaa.Fliplr(0.5),
                iaa.CropAndPad(px=(0, 112), sample_independently=False),
                iaa.Affine(translate_percent={"x": (-0.4, 0.4), "y": (-0.4, 0.4)}),
                iaa.SomeOf((0, 3), [
                    iaa.AddToHueAndSaturation((-10, 10)),
                    iaa.Affine(scale={"x": (0.9, 1.1), "y": (0.9, 1.1)}),
                    iaa.GaussianBlur(sigma=(0, 1.0)),
                    iaa.AdditiveGaussianNoise(scale=0.05 * 255)
                ])
            ])
        elif stage == "val":
            return iaa.Sequential([
                iaa.CropAndPad(px=(0, 112), sample_independently=False),
                iaa.Affine(translate_percent={"x": (-0.4, 0.4), "y": (-0.4, 0.4)}),
            ])
        elif stage == "test":
            return iaa.Sequential([])


@plac.annotations(
    batch_size=('The training batch size', 'option', 'B', int),
    epochs=('Number of epochs to train', 'option', 'E', int),
    train_path=(
            'Path to the train folder which contains both an images and labels folder with KITTI labels',
            'option', 'T', str),
    val_path=(
            'Path to the validation folder which contains both an images and labels folder with KITTI labels',
            'option', 'V', str),
    weights=('Weights file to start with', 'option', 'W', str),
    multi_gpu_weights=('Weights file to start with for the multi GPU model', 'option', 'G', str),
    workers=('Number of fit_generator workers', 'option', 'w', int),
    find_lr=('Instead of training, search for an optimal learning rate', 'flag', None, bool),
    model=('Which model architecture to train (complete, coverage, regions, pooling)', 'option', 'M', str)
)
def main(batch_size: int = 24,
         epochs: int = 384,
         train_path: str = 'train',
         val_path: str = 'val',
         model: str = 'complete',
         multi_gpu_weights=None,
         weights=None,
         workers: int = 8,
         find_lr: bool = False):

    if model is None or model == "complete":
        keras_model = MobileDetectNetModel.complete_model()
        raise Exception("Not implemented yet!")
    elif model == "coverage":
        keras_model = MobileDetectNetModel.coverage_model()
    elif model == "region":
        keras_model = MobileDetectNetModel.region_model()
        raise Exception("Not implemented yet!")
    elif model == "pooling":
        keras_model = MobileDetectNetModel.pooling_model()
        raise Exception("Not implemented yet!")
    else:
        raise Exception("Invalid mode: %s" % model)

    keras_model.summary()
    if weights is not None:
        keras_model.load_weights(weights)

    coverage_shape = [14, 14]
    bboxes_shape = [14, 14]

    train_seq = MobileDetectNetSequence(train_path, stage="train", batch_size=batch_size,
                                        coverage_height=coverage_shape[0], coverage_width=coverage_shape[1],
                                        bboxes_height=bboxes_shape[0], bboxes_width=bboxes_shape[1],
                                        model=model)
    val_seq = MobileDetectNetSequence(val_path, stage="val", batch_size=batch_size,
                                      coverage_height=coverage_shape[0], coverage_width=coverage_shape[1],
                                      bboxes_height=bboxes_shape[0], bboxes_width=bboxes_shape[1],
                                      model=model)

    keras_model = keras.utils.multi_gpu_model(keras_model, gpus=[0, 1], cpu_merge=True, cpu_relocation=False)
    if multi_gpu_weights is not None:
        keras_model.load_weights(multi_gpu_weights)

    if model is None or model == "complete":
        raise Exception("Not implemented yet!")
    elif model == "coverage":
        keras_model.compile(optimizer=SGD(), loss='mean_absolute_error')
    elif model == "region":
        raise Exception("Not implemented yet!")
    elif model == "pooling":
        raise Exception("Not implemented yet!")
    else:
        raise Exception("Invalid mode: %s" % model)

    if find_lr:
        from lr_finder import LRFinder
        lr_finder = LRFinder(keras_model)
        lr_finder.find_generator(train_seq, start_lr=0.000001, end_lr=1, epochs=5)
        lr_finder.plot_loss()
        return

    filepath = "weights-%s-{epoch:02d}-{val_loss:.4f}-multi-gpu.hdf5" % model
    checkpoint = ModelCheckpoint(filepath, monitor='val_loss', verbose=1, save_best_only=True, mode='min')

    sgdr_sched = SGDRScheduler(0.00001, 0.01, steps_per_epoch=np.ceil(len(train_seq) / batch_size), mult_factor=1.5)

    keras_model.fit_generator(train_seq,
                        validation_data=val_seq,
                        epochs=epochs,
                        steps_per_epoch=np.ceil(len(train_seq) / batch_size),
                        validation_steps=np.ceil(len(val_seq) / batch_size),
                        callbacks=[checkpoint, sgdr_sched],
                        use_multiprocessing=True,
                        workers=workers,
                        shuffle=True)


if __name__ == '__main__':
    plac.call(main)
