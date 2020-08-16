# MIT License
#
# Copyright (C) The Adversarial Robustness Toolbox (ART) Authors 2018
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
# persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
# Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""
This module implements the adversarial patch attack `AdversarialPatch`. This attack generates an adversarial patch that
can be printed into the physical world with a common printer. The patch can be used to fool image classifiers.

| Paper link: https://arxiv.org/abs/1712.09665
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import logging
from typing import Optional, Union

import random
import numpy as np
from scipy.ndimage import rotate, shift, zoom
from tqdm import trange

from art.attacks.attack import EvasionAttack
from art.estimators.estimator import BaseEstimator, NeuralNetworkMixin
from art.estimators.classification.classifier import (
    ClassifierMixin,
    ClassifierNeuralNetwork,
    ClassifierGradients,
)
from art.utils import check_and_transform_label_format

logger = logging.getLogger(__name__)


class AdversarialPatchNumpy(EvasionAttack):
    """
    Implementation of the adversarial patch attack.

    | Paper link: https://arxiv.org/abs/1712.09665
    """

    attack_params = EvasionAttack.attack_params + [
        "rotation_max",
        "scale_min",
        "scale_max",
        "learning_rate",
        "max_iter",
        "batch_size",
    ]

    _estimator_requirements = (BaseEstimator, NeuralNetworkMixin, ClassifierMixin)

    def __init__(
        self,
        classifier: Union[ClassifierNeuralNetwork, ClassifierGradients],
        target: int = 0,
        rotation_max: float = 22.5,
        scale_min: float = 0.1,
        scale_max: float = 1.0,
        learning_rate: float = 5.0,
        max_iter: int = 500,
        clip_patch: Union[list, tuple, None] = None,
        batch_size: int = 16,
    ) -> None:
        """
        Create an instance of the :class:`.AdversarialPatchNumpy`.

        :param classifier: A trained classifier.
        :param target: The target label for the created patch.
        :param rotation_max: The maximum rotation applied to random patches. The value is expected to be in the
               range `[0, 180]`.
        :param scale_min: The minimum scaling applied to random patches. The value should be in the range `[0, 1]`,
               but less than `scale_max`.
        :param scale_max: The maximum scaling applied to random patches. The value should be in the range `[0, 1]`, but
               larger than `scale_min.`
        :param learning_rate: The learning rate of the optimization.
        :param max_iter: The number of optimization steps.
        :param clip_patch: The minimum and maximum values for each channel in the form
               [(float, float), (float, float), (float, float)].
        :param batch_size: The size of the training batch.
        """
        super(AdversarialPatchNumpy, self).__init__(estimator=classifier)

        self.target = target
        self.rotation_max = rotation_max
        self.scale_min = scale_min
        self.scale_max = scale_max
        self.learning_rate = learning_rate
        self.max_iter = max_iter
        self.batch_size = batch_size
        self.clip_patch = clip_patch
        self._check_params()

        self.image_shape = self.estimator.input_shape

        if self.estimator.channels_first:
            self.i_h = 1
            self.i_w = 2
        else:
            self.i_h = 0
            self.i_w = 1

        if self.estimator.channels_first:
            smallest_image_edge = np.minimum(self.image_shape[1], self.image_shape[2])
            nb_channels = self.image_shape[0]
            self.patch_shape = (nb_channels, smallest_image_edge, smallest_image_edge)
        else:
            smallest_image_edge = np.minimum(self.image_shape[0], self.image_shape[1])
            nb_channels = self.image_shape[2]
            self.patch_shape = (smallest_image_edge, smallest_image_edge, nb_channels)

        self.patch_shape = self.image_shape

        mean_value = (self.estimator.clip_values[1] - self.estimator.clip_values[0]) / 2.0 + self.estimator.clip_values[
            0
        ]
        self.patch = np.ones(shape=self.patch_shape).astype(np.float32) * mean_value

    def generate(self, x: np.ndarray, y: Optional[np.ndarray] = None, **kwargs) -> np.ndarray:
        """
        Generate adversarial samples and return them in an array.

        :param x: An array with the original inputs. `x` is expected to have spatial dimensions.
        :param y: An array with the original labels to be predicted.
        :return: An array holding the adversarial patch.
        """
        logger.info("Creating adversarial patch.")

        if len(x.shape) == 2:
            raise ValueError(
                "Feature vectors detected. The adversarial patch can only be applied to data with spatial "
                "dimensions."
            )

        y_target = check_and_transform_label_format(labels=y, nb_classes=self.estimator.nb_classes)

        for _ in trange(self.max_iter, desc="Adversarial Patch Numpy"):
            patched_images, patch_mask_transformed, transforms = self._augment_images_with_random_patch(x, self.patch)

            num_batches = int(x.shape[0] / self.batch_size)
            patch_gradients = np.zeros_like(self.patch)

            for i_batch in range(num_batches):
                i_batch_start = i_batch * self.batch_size
                i_batch_end = (i_batch + 1) * self.batch_size

                gradients = self.estimator.loss_gradient(
                    patched_images[i_batch_start:i_batch_end], y_target[i_batch_start:i_batch_end],
                )

                for i_image in range(self.batch_size):
                    patch_gradients_i = self._reverse_transformation(
                        gradients[i_image, :, :, :], patch_mask_transformed[i_image, :, :, :], transforms[i_image],
                    )
                    patch_gradients += patch_gradients_i

            # patch_gradients = patch_gradients / (num_batches * self.batch_size)
            self.patch -= patch_gradients * self.learning_rate
            self.patch = np.clip(self.patch, a_min=self.estimator.clip_values[0], a_max=self.estimator.clip_values[1],)

        return self.patch, self._get_circular_patch_mask()

    def apply_patch(self, x: np.ndarray, scale: float, patch_external: np.ndarray = None) -> np.ndarray:
        """
        A function to apply the learned adversarial patch to images.

        :param x: Instances to apply randomly transformed patch.
        :param scale: Scale of the applied patch in relation to the classifier input shape.
        :param patch_external: External patch to apply to images `x`.
        :return: The patched instances.
        """
        patch = patch_external if patch_external is not None else self.patch
        patched_x, _, _ = self._augment_images_with_random_patch(x, patch, scale)
        return patched_x

    def _check_params(self) -> None:
        if not isinstance(self.rotation_max, (float, int)):
            raise ValueError("The maximum rotation of the random patches must be of type float.")
        if self.rotation_max < 0 or self.rotation_max > 180.0:
            raise ValueError("The maximum rotation of the random patches must be between 0 and 180 degrees.")

        if not isinstance(self.scale_min, float):
            raise ValueError("The minimum scale of the random patched must be of type float.")
        if self.scale_min < 0 or self.scale_min > self.scale_max:
            raise ValueError(
                "The minimum scale of the random patched must be greater than 0 and less than the maximum scaling."
            )

        if not isinstance(self.scale_max, float):
            raise ValueError("The maximum scale of the random patched must be of type float.")
        if self.scale_max > 1:
            raise ValueError("The maximum scale of the random patched must not be greater than 1.")

        if not isinstance(self.learning_rate, float):
            raise ValueError("The learning rate must be of type float.")
        if not self.learning_rate > 0.0:
            raise ValueError("The learning rate must be greater than 0.0.")

        if not isinstance(self.max_iter, int):
            raise ValueError("The number of optimization steps must be of type int.")
        if not self.max_iter > 0:
            raise ValueError("The number of optimization steps must be greater than 0.")

        if not isinstance(self.batch_size, int):
            raise ValueError("The batch size must be of type int.")
        if not self.batch_size > 0:
            raise ValueError("The batch size must be greater than 0.")

    def _get_circular_patch_mask(self, sharpness: int = 40) -> np.ndarray:
        """
        Return a circular patch mask
        """
        diameter = np.minimum(self.patch_shape[self.i_h], self.patch_shape[self.i_w])

        x = np.linspace(-1, 1, diameter)
        y = np.linspace(-1, 1, diameter)
        x_grid, y_grid = np.meshgrid(x, y, sparse=True)
        z_grid = (x_grid ** 2 + y_grid ** 2) ** sharpness

        mask = 1 - np.clip(z_grid, -1, 1)

        pad_h_before = int((self.image_shape[self.i_h] - mask.shape[self.i_h]) / 2)
        pad_h_after = int(self.image_shape[self.i_h] - pad_h_before - mask.shape[self.i_h])

        pad_w_before = int((self.image_shape[self.i_w] - mask.shape[self.i_w]) / 2)
        pad_w_after = int(self.image_shape[self.i_w] - pad_w_before - mask.shape[self.i_w])

        mask = np.pad(
            mask,
            pad_width=((pad_h_before, pad_h_after), (pad_w_before, pad_w_after)),
            mode="constant",
            constant_values=(0, 0),
        )

        channel_index = 1 if self.estimator.channels_first else 3
        axis = channel_index - 1
        mask = np.expand_dims(mask, axis=axis)
        mask = np.broadcast_to(mask, self.patch_shape).astype(np.float32)
        return mask

    def _augment_images_with_random_patch(self, images, patch, scale=None):
        """
        Augment images with randomly rotated, shifted and scaled patch.
        """
        transformations = list()
        patched_images = list()
        patch_mask_transformed_list = list()

        for i_image in range(images.shape[0]):
            (patch_transformed, patch_mask_transformed, transformation,) = self._random_transformation(patch, scale)

            inverted_patch_mask_transformed = 1 - patch_mask_transformed

            patched_image = (
                images[i_image, :, :, :] * inverted_patch_mask_transformed + patch_transformed * patch_mask_transformed
            )
            patched_image = np.expand_dims(patched_image, axis=0)
            patched_images.append(patched_image)

            patch_mask_transformed = np.expand_dims(patch_mask_transformed, axis=0)
            patch_mask_transformed_list.append(patch_mask_transformed)
            transformations.append(transformation)

        patched_images = np.concatenate(patched_images, axis=0)
        patch_mask_transformed_np = np.concatenate(patch_mask_transformed_list, axis=0)

        return patched_images, patch_mask_transformed_np, transformations

    def _rotate(self, x, angle):
        axes = (self.i_h, self.i_w)
        return rotate(x, angle=angle, reshape=False, axes=axes, order=1)

    def _scale(self, x, scale):
        zooms = None
        height = None
        width = None
        if self.estimator.channels_first:
            zooms = (1.0, scale, scale)
            height, width = self.patch_shape[1:3]
        elif not self.estimator.channels_first:
            zooms = (scale, scale, 1.0)
            height, width = self.patch_shape[0:2]

        if scale < 1.0:
            scale_h = int(np.round(height * scale))
            scale_w = int(np.round(width * scale))
            top = (height - scale_h) // 2
            left = (width - scale_w) // 2

            x_out = np.zeros_like(x)
            x_out[top : top + scale_h, left : left + scale_w] = zoom(x, zoom=zooms, order=1)

            if self.estimator.channels_first:
                x_out[:, top : top + scale_h, left : left + scale_w] = zoom(x, zoom=zooms, order=1)
            else:
                x_out[top : top + scale_h, left : left + scale_w, :] = zoom(x, zoom=zooms, order=1)

        elif scale > 1.0:
            scale_h = int(np.round(height / scale))
            scale_w = int(np.round(width / scale))
            top = (height - scale_h) // 2
            left = (width - scale_w) // 2

            x_out = zoom(x[top : top + scale_h, left : left + scale_w], zoom=zooms, order=1)

            cut_top = (x_out.shape[self.i_h] - height) // 2
            cut_left = (x_out.shape[self.i_w] - width) // 2

            if self.estimator.channels_first:
                x_out = x_out[:, cut_top : cut_top + height, cut_left : cut_left + width]
            else:
                x_out = x_out[cut_top : cut_top + height, cut_left : cut_left + width, :]

        else:
            x_out = x

        return x_out

    def _shift(self, x, shift_h, shift_w):
        if self.estimator.channels_first:
            shift_hw = (0, shift_h, shift_w)
        else:
            shift_hw = (shift_h, shift_w, 0)

        x = shift(x, shift=shift_hw, order=1)
        return x, shift_h, shift_w

    def _random_transformation(self, patch, scale):
        patch_mask = self._get_circular_patch_mask()
        transformation = dict()

        # rotate
        angle = random.uniform(-self.rotation_max, self.rotation_max)
        transformation["rotate"] = angle
        patch = self._rotate(patch, angle)
        patch_mask = self._rotate(patch_mask, angle)

        # scale
        if scale is None:
            scale = random.uniform(self.scale_min, self.scale_max)
        patch = self._scale(patch, scale)
        patch_mask = self._scale(patch_mask, scale)
        transformation["scale"] = scale

        # shift
        shift_max_h = (self.estimator.input_shape[self.i_h] - self.patch_shape[self.i_h] * scale) / 2.0
        shift_max_w = (self.estimator.input_shape[self.i_w] - self.patch_shape[self.i_w] * scale) / 2.0
        if shift_max_h > 0 and shift_max_w > 0:
            shift_h = random.uniform(-shift_max_h, shift_max_h)
            shift_w = random.uniform(-shift_max_w, shift_max_w)
            patch, _, _ = self._shift(patch, shift_h, shift_w)
            patch_mask, shift_1, shift_2 = self._shift(patch_mask, shift_h, shift_w)
            transformation["shift_1"] = shift_1
            transformation["shift_2"] = shift_2
        else:
            transformation["shift"] = (0, 0, 0)

        return patch, patch_mask, transformation

    def _reverse_transformation(self, gradients: np.ndarray, patch_mask_transformed, transformation) -> np.ndarray:
        gradients = gradients * patch_mask_transformed

        # shift
        shift_h = transformation["shift_h"]
        shift_w = transformation["shift_w"]
        gradients, _, _ = self._shift(gradients, -shift_h, -shift_w)

        # scale
        scale = transformation["scale"]
        gradients = self._scale(gradients, 1.0 / scale)

        # rotate
        angle = transformation["rotate"]
        gradients = self._rotate(gradients, -angle)
        return gradients
