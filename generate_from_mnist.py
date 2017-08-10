from config import DATA_PATH
import numpy as np
import os, sys
from scipy import misc

import keras.backend as K
import tensorflow as tf

from src.attackers.deepfool import DeepFool
from src.attackers.fast_gradient import FastGradientMethod
from src.attackers.saliency_map import SaliencyMapMethod
from src.attackers.universal_perturbation import UniversalPerturbation

from src.classifiers.utils import load_classifier

from src.utils import make_directory, get_label_conf, load_dataset

PATH = "./mnist/"

adv_method = sys.argv[1]

assert adv_method in ["fgsm", "deepfool", "universal", "jsma"]

session = tf.Session()
K.set_session(session)

# get dataset
_, (X, Y) = load_dataset("mnist")
X, Y = X[:1], Y[:1]

# load cnn classifier
our_classifier = load_classifier(os.path.join(DATA_PATH, "classifiers", "mnist", "cnn", "brelu", "gaussian",
                                              "stdev0.30", "pert-insts10"), "best-weights.h5")
basic_classifier = load_classifier(os.path.join(DATA_PATH, "classifiers", "mnist", "cnn", "relu"), "best-weights.h5")

save_path = os.path.join(PATH, adv_method)

if adv_method == "universal":

    attack_params = {"clip_min": 0.,
                     "clip_max": 1}

    attack_on_our = UniversalPerturbation(our_classifier, session, p=np.inf, attacker_params=attack_params)
    attack_on_basic = UniversalPerturbation(basic_classifier, session, p=np.inf, attacker_params=attack_params)

elif adv_method == "deepfool":

    attack_params = {"clip_min": 0.,
                     "clip_max": 1,}

    attack_on_our = DeepFool(our_classifier, session, max_iter=10, verbose=2)
    attack_on_basic = DeepFool(basic_classifier, session, max_iter=10, verbose=2)

elif adv_method == "jsma":

    attack_params = {}
    attack_on_our = SaliencyMapMethod(our_classifier, sess=session, clip_min=0., clip_max=1., gamma=1., theta=0.1)
    attack_on_basic = SaliencyMapMethod(basic_classifier, sess=session, clip_min=0., clip_max=1., gamma=1., theta=0.1)


else:

    try:
        eps = int(sys.argv[2])

        attack_params = {"clip_min": 0.,
                         "clip_max": 1,
                         "eps": eps}

        save_path = os.path.join(save_path, "eps%d" % eps)

    except:

        attack_params = {"clip_min": 0.,
                         "clip_max": 1,
                         "minimal": True,
                         "eps_step": 0.02,
                         "eps_max": 1}

        save_path = os.path.join(save_path, "minimal")


    attack_on_our = FastGradientMethod(our_classifier, session)
    attack_on_basic = FastGradientMethod(basic_classifier, session)

save_path_our = os.path.join(save_path, "gaussian_brelu")
make_directory(save_path_our)
save_path_basic = os.path.join(save_path, "vanilla")
make_directory(save_path_basic)

advs_on_our = attack_on_our.generate(X, **attack_params)
print(advs_on_our)
advs_on_basic = attack_on_basic.generate(X, **attack_params)

make_directory(save_path)

for i, (adv_our, adv_basic) in enumerate(zip(advs_on_our, advs_on_basic)):
    misc.toimage(adv_our[:,:,0], cmin=0.0, cmax=1.).save(os.path.join(save_path_our, "{}.jpg".format(i)))
    misc.toimage(adv_basic[:,:,0], cmin=0.0, cmax=1.).save(os.path.join(save_path_basic, "{}.jpg".format(i)))

    # print("original", get_label_conf(model.predict(X[i][None, ...])))
    # print("adversarial", get_label_conf(model.predict(adv[None, ...])))