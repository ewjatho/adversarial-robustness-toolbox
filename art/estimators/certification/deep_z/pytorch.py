# MIT License
#
# Copyright (C) The Adversarial Robustness Toolbox (ART) Authors 2022
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
This module implements DeepZ proposed in Fast and Effective Robustness Certification.

| Paper link: https://papers.nips.cc/paper/2018/file/f2f446980d8e971ef3da97af089481c3-Paper.pdf
"""

from typing import List, Optional, Tuple, Union, TYPE_CHECKING

import warnings
import numpy as np
import torch

from art.estimators.certification.deep_z.deep_z import ZonoConv, ZonoDenseLayer, ZonoReLU, ZonoBounds
from art.estimators.classification.pytorch import PyTorchClassifier

if TYPE_CHECKING:
    from art.utils import CLIP_VALUES_TYPE, PREPROCESSING_TYPE
    from art.defences.preprocessor import Preprocessor
    from art.defences.postprocessor import Postprocessor


class ConvertedModel(torch.nn.Module):
    """
    Class which converts the supplied pytorch model into an equivalent model
    which uses abstract operations
    """

    def __init__(self, model: "torch.nn.Module", channels_first: bool, input_shape: Tuple[int, ...]):
        super().__init__()
        modules = []
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.forward_mode: str
        self.forward_mode = "abstract"

        # pylint: disable=W0613
        def forward_hook(input_module, hook_input, hook_output):
            modules.append(input_module)

        for module in model.children():
            module.register_forward_hook(forward_hook)

        if channels_first:
            input_for_hook = torch.rand(input_shape).to(device)
        else:
            raise ValueError("Please provide data in channels first format")

        input_for_hook = torch.unsqueeze(input_for_hook, dim=0)
        model(input_for_hook)  # hooks are fired sequentially from model input to the output

        self.ops = torch.nn.ModuleList()
        for module in modules:
            print("registered", type(module))
            if isinstance(module, torch.nn.modules.conv.Conv2d):
                zono_conv = ZonoConv(
                    in_channels=module.in_channels,
                    out_channels=module.out_channels,
                    kernel_size=module.kernel_size,  # type: ignore
                    stride=module.stride,  # type: ignore
                    dilation=module.dilation,  # type: ignore
                    padding=module.padding,  # type: ignore
                )
                zono_conv.conv.weight.data = module.weight.data.to(device)
                zono_conv.bias.data = module.bias.data.to(device)  # type: ignore
                self.ops.append(zono_conv)

            elif isinstance(module, torch.nn.modules.linear.Linear):
                zono_dense = ZonoDenseLayer(in_features=module.in_features, out_features=module.out_features)
                zono_dense.weight.data = module.weight.data.to(device)
                zono_dense.bias.data = module.bias.data.to(device)
                self.ops.append(zono_dense)

            elif isinstance(module, torch.nn.modules.activation.ReLU):
                self.ops.append(ZonoReLU(device=device))
            else:
                raise ValueError("Supported Operations are Conv2D, Linear, and RelU")

        for op_num, op in enumerate(self.ops):
            # as reshapes are not modules we infer when the reshape from convolutional to dense occurs
            if isinstance(op, ZonoDenseLayer):
                # if the preceeding op was a convolution:
                if isinstance(self.ops[op_num - 1], ZonoConv):
                    self.reshape_op_num = op_num
                    print("Inferred reshape on op num", op_num)
                # if the preceeding op was a relu and the one before the activation was a convolution
                if isinstance(self.ops[op_num - 1], ZonoReLU) and isinstance(self.ops[op_num - 2], ZonoConv):
                    self.reshape_op_num = op_num
                    print("Inferred reshape on op num", op_num)

    def forward(
        self, cent: np.ndarray, eps: Optional[np.ndarray] = None
    ) -> Union["torch.Tensor", Tuple["torch.Tensor", "torch.Tensor"]]:
        """
        Performs the neural network forward pass, either using abstract operations or concrete ones
        depending on the value of self.forward_mode

        :param cent: input data, either regular data if running in concrete mode, or the zonotope bias term.
        :param eps: zonotope error terms if running in abstract mode

        :return: model predictions, with zonotope error terms if running in abstract mode
        """
        if self.forward_mode == "concrete":
            return self.concrete_forward(cent)
        if self.forward_mode == "abstract" and eps is not None:
            out_cent, out_eps = self.abstract_forward(cent, eps)
            return out_cent, out_eps
        raise ValueError("forward_mode must be set to abstract or concrete")

    def abstract_forward(self, cent: np.ndarray, eps: np.ndarray) -> Tuple["torch.Tensor", "torch.Tensor"]:
        """
        Do the forward pass through the NN with the given error terms and zonotope center.

        :param eps: Error terms of the zonotope.
        :param cent: The datapoint, representing the zonotope center.
        :return: A tuple, the first element being the zonotope center vector.
                 The second is the zonotope error terms/coefficients.
        """
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

        x = np.concatenate([cent, eps])
        x = torch.from_numpy(x.astype("float32")).to(device)

        for op_num, op in enumerate(self.ops):
            # as reshapes are not modules we infer when the reshape from convolutional to dense occurs
            if self.reshape_op_num == op_num:
                x = x.reshape((x.shape[0], -1))
            x = op(x)

        return x[0, :], x[1:, :]

    def concrete_forward(self, in_x: Union[np.ndarray, "torch.Tensor"]) -> "torch.Tensor":
        """
        Do the forward pass using the concrete operations

        :param in_x: regular (concrete) data.
        """
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        if isinstance(in_x, np.ndarray):
            x = torch.from_numpy(in_x.astype("float32")).to(device)
        else:
            x = in_x

        for op_num, op in enumerate(self.ops):
            # as reshapes are not modules we infer when the reshape from convolutional to dense occurs
            if self.reshape_op_num == op_num:
                x = x.reshape((x.shape[0], -1))
            x = op.concrete_forward(x)
        return x


class PytorchDeepZ(PyTorchClassifier, ZonoBounds):
    """
    Implementation of DeepZ to certify neural network robustness. We use the zonotope representation of a datapoint as
    it travels through the network to then verify if it can have its class changed given a certain perturbation.

    | Paper link: https://papers.nips.cc/paper/2018/file/f2f446980d8e971ef3da97af089481c3-Paper.pdf
    """

    estimator_params = PyTorchClassifier.estimator_params

    def __init__(
        self,
        model: "torch.nn.Module",
        loss: "torch.nn.modules.loss._Loss",
        input_shape: Tuple[int, ...],
        nb_classes: int,
        optimizer: Optional["torch.optim.Optimizer"] = None,  # type: ignore
        channels_first: bool = True,
        clip_values: Optional["CLIP_VALUES_TYPE"] = None,
        preprocessing_defences: Union["Preprocessor", List["Preprocessor"], None] = None,
        postprocessing_defences: Union["Postprocessor", List["Postprocessor"], None] = None,
        preprocessing: "PREPROCESSING_TYPE" = (0.0, 1.0),
        device_type: str = "gpu",
    ):
        """
        Create a certifier based on the zonotope domain.

        :param model: PyTorch model. The output of the model can be logits, probabilities or anything else. Logits
               output should be preferred where possible to ensure attack efficiency.
        :param loss: The loss function for which to compute gradients for training. The target label must be raw
               categorical, i.e. not converted to one-hot encoding.
        :param input_shape: The shape of one input instance.
        :param nb_classes: The number of classes of the model.
        :param optimizer: The optimizer used to train the classifier.
        :param channels_first: Set channels first or last.
        :param clip_values: Tuple of the form `(min, max)` of floats or `np.ndarray` representing the minimum and
               maximum values allowed for features. If floats are provided, these will be used as the range of all
               features. If arrays are provided, each value will be considered the bound for a feature, thus
               the shape of clip values needs to match the total number of features.
        :param preprocessing_defences: Preprocessing defence(s) to be applied by the classifier.
        :param postprocessing_defences: Postprocessing defence(s) to be applied by the classifier.
        :param preprocessing: Tuple of the form `(subtrahend, divisor)` of floats or `np.ndarray` of values to be
               used for data preprocessing. The first value will be subtracted from the input. The input will then
               be divided by the second one.
        :param device_type: Type of device on which the classifier is run, either `gpu` or `cpu`.
        """

        warnings.warn(
            "\nThis estimator does not support networks which have dense layers before convolutional. "
            "We currently infer a reshape when a neural network goes from convolutional layers to "
            "dense layers. If your use case does not fall into this pattern then consider "
            "directly building a certifier network with the "
            "custom layers found in art.estimators.certification.deepz.deep_z.py\n"
        )
        converted_model = ConvertedModel(model, channels_first, input_shape)

        if TYPE_CHECKING:
            converted_optimizer: Union[torch.optim.Adam, torch.optim.SGD, None]

        if optimizer is not None:
            opt_state_dict = optimizer.state_dict()
            if isinstance(optimizer, torch.optim.Adam):
                print("Converting Adam Optimiser")
                converted_optimizer = torch.optim.Adam(converted_model.parameters(), lr=1e-4)
            elif isinstance(optimizer, torch.optim.SGD):
                print("Converting SGD Optimiser")
                converted_optimizer = torch.optim.SGD(converted_model.parameters(), lr=1e-4)
            else:
                raise ValueError("Optimiser not supported for conversion")

            converted_optimizer.load_state_dict(opt_state_dict)
        else:
            converted_optimizer = None

        super().__init__(
            model=converted_model,
            loss=loss,
            input_shape=input_shape,
            nb_classes=nb_classes,
            optimizer=converted_optimizer,
            channels_first=channels_first,
            clip_values=clip_values,
            preprocessing_defences=preprocessing_defences,
            postprocessing_defences=postprocessing_defences,
            preprocessing=preprocessing,
            device_type=device_type,
        )

    def forward(self, cent: np.ndarray, eps: np.ndarray) -> Tuple["torch.Tensor", "torch.Tensor"]:
        """
        Do the forward pass through the NN with the given error terms and zonotope center.

        :param eps: Error terms of the zonotope.
        :param cent: The datapoint, representing the zonotope center.
        :return: A tuple, the first element being the zonotope center vector.
                 The second is the zonotope error terms/coefficients.
        """
        out_cent, out_eps = self.model.forward(cent, eps)
        return out_cent, out_eps

    def set_forward_mode(self, mode: str) -> None:
        """
        Helper function to set the forward mode of the model

        :param mode: either concrete or abstract signifying how to run the forward pass
        """
        assert mode in ["concrete", "abstract"]
        self.model.forward_mode = mode

    def certify(self, cent: np.ndarray, eps: np.ndarray, prediction: int) -> bool:
        """
        Check if the datapoint has been certifiably classified.

        First do the forward pass through the NN with the given error terms and zonotope center to
        obtain the output zonotope.

        Then perform the certification step by computing the difference of the logits in the final zonotope
        and projecting to interval.

        :param eps: Error terms of the zonotope.
        :param cent: The datapoint, representing the zonotope center.
        :param prediction: The prediction the neural network gave on the basic datapoint.

        :return: True/False if the datapoint could be misclassified given the eps bounds.
        """
        cent_tensor, eps_tensor = self.forward(eps=eps, cent=cent)
        cent = cent_tensor.detach().cpu().numpy()
        eps = eps_tensor.detach().cpu().numpy()

        certification_results = []
        for k in range(self.nb_classes):
            if k != prediction:
                cert_via_sub = self.certify_via_subtraction(
                    predicted_class=prediction, class_to_consider=k, cent=cent, eps=eps
                )
                certification_results.append(cert_via_sub)

        return all(certification_results)

    def concrete_loss(self, output, target):
        """
        Access function to get the classifier loss

        :param output:
        :param target:

        :return:
        """
        return self._loss(output, target)

    def apply_preprocessing(self, x, y, fit):
        """
        Access function to get preprocessing

        :param x:
        :param y:
        :param fit:

        :return:
        """
        x_preprocessed, y_preprocessed = self._apply_preprocessing(x, y, fit=fit)
        return x_preprocessed, y_preprocessed

    @staticmethod
    def max_logit_loss(output, target):
        """
        Computes the loss as the largest logit value amongst the incorrect classes.
        """
        target_logit = output[:, target]
        output = output - target_logit

        ubs = torch.sum(torch.abs(output[1:, :]), axis=0) + output[0, :]

        loss = None
        for i in range(10):
            if i != target and (loss is None or ubs[i] > loss):
                loss = ubs[i]
        return loss

    @staticmethod
    def get_accuracy(preds: Union[np.ndarray, "torch.Tensor"], labels: Union[np.ndarray, "torch.Tensor"]) -> np.ndarray:
        """
        Helper function to print out the accuracy during training

        :param preds: (concrete) model predictions
        :param labels: ground truth labels (not one hot)

        :return: prediction accuracy
        """
        if isinstance(preds, torch.Tensor):
            preds = preds.detach().cpu().numpy()

        if isinstance(labels, torch.Tensor):
            labels = labels.detach().cpu().numpy()

        return np.sum(np.argmax(preds, axis=1) == labels) / len(labels)
