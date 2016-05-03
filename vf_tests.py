#!/usr/bin/env python3
import pyximport; pyximport.install()
import numpy as np
from sklearn import preprocessing
from sklearn import ensemble
from sklearn import cross_validation
from sklearn import metrics
from sklearn import grid_search
import vf_data
from vf_features import load_features
from vf_eval import *
import multiprocessing as mp
import csv
import argparse
from array import array


NON_SHOCKABLE = 0
SHOCKABLE = 1
INTERMEDIATE = 2
aha_classes = (NON_SHOCKABLE, SHOCKABLE, INTERMEDIATE)
aha_classe_names = ["non-shockable", "shockable", "intermediate"]

# Use threshold value: 180 BPM to define rapid VT
# Reference: Nishiyama et al. 2015. Diagnosis of Automated External Defibrillators (JAHA)
RAPID_VT_RATE = 180

# 0.2 mV is suggested by AHA
COARSE_VF_THRESHOLD = 0.2

# label the segment according to different problem definition
label_methods_desc = """
label methods:
aha: multi-class based on AHA guideline for AED:
    shockable (coarse VF + rapid VT): 1
    intermediate (fine VF + slow VT) :2
    non-shockable (others): 0
0: binary => VF: 1, others: 0
1: binary => VF or VFL: 1, others: 0
2: binary => VF or VFL or VT: 1, others: 0
3: multi-class:
    VF: 1
    VFL/VT: 2
    others: 0
"""


def make_labels(x_data_info, label_method):
    y_data = array('I')
    for info in x_data_info:
        rhythm = info.rhythm
        label = 0
        if label_method == "aha":
            # distinguish subtypes of VT and VF
            # References for the definition of "coarse":
            # 1. Foundations of Respiratory Care. by Kenneth A. Wyka，Paul J. Mathews，John Rutkowski
            #    Chapter 19. p.537
            #    Quote: "Coarse VF exists when wave amplitude is more than 3 mm."
            # 2. ECGs Made Easy by Barbara J Aehlert
            #    p.203
            #    Quote: "Coarse VF is 3 mm or more in amplitude. Fine VF is less than 3 mm in amplitude."
            # 3. In AHA recommendations for AED, a peak-to-peak amplitude of 0.2 mV is suggested.
            if rhythm == "(VF":
                if info.amplitude > COARSE_VF_THRESHOLD:  # coarse VF
                    label = SHOCKABLE
                else:  # fine VF
                    label = INTERMEDIATE
            elif rhythm == "(VT":
                hr = info.get_heart_rate()
                if hr >= RAPID_VT_RATE:
                    label = SHOCKABLE
                elif hr > 0:
                    label = INTERMEDIATE
            elif rhythm == "(VFL":  # VFL is VF with HR > 240 BPM, so it's kind of rapid VT
                label = SHOCKABLE
        elif label_method == "0":
            label = 1 if rhythm == "(VF" else 0
        elif label_method == "1":
            label = 1 if rhythm in ("(VF", "(VFL") else 0
        elif label_method == "2":
            label = 1 if rhythm in ("(VF", "(VFL", "(VT") else 0
        elif label_method == "3":  # multi-class: VF, VFL/VT, others
            if rhythm == "(VF":
                label = 1
            elif rhythm in ("(VT", "(VFL"):  # We define VFL as rapid VT here.
                # VT at 240-300 beats/min is often termed ventricular flutter.
                # http://emedicine.medscape.com/article/159075-overview
                label = 2
            else:  # others
                label = 0
        y_data.append(label)
    return np.array(y_data)


def get_sample_weights(y_data):
    classes = np.unique(y_data)
    n_classes = [np.sum([y_data == k]) for k in classes]
    n_total = len(y_data)
    weights = np.zeros(y_data)
    for k, n in zip(classes, n_classes):
        weights[y_data == k] = (n_total / n)
    return weights


def main():
    # parse command line arguments
    parser = argparse.ArgumentParser()
    # known estimators
    estimator_names = ("logistic_regression", "random_forest", "adaboost", "gradient_boosting", "svc", "mlp1", "mlp2")
    parser.add_argument("-m", "--model", type=str, required=True, choices=estimator_names)
    parser.add_argument("-i", "--input", type=str, required=True)
    parser.add_argument("-o", "--output", type=str, required=True)
    parser.add_argument("-j", "--jobs", type=int, default=-1)
    parser.add_argument("-t", "--iter", type=int, default=1)
    parser.add_argument("-s", "--scorer", type=str, choices=("ber", "f1", "accuracy", "precision", "f1_weighted"), default="f1_weighted")
    parser.add_argument("-c", "--cv-fold", type=int, default=5)  # 5 fold CV by default
    parser.add_argument("-p", "--test-percent", type=int, default=30)  # 30% test set size
    parser.add_argument("-b", "--balanced-weight", action="store_true")  # used balanced class weighting
    parser.add_argument("-f", "--features", type=int, nargs="+")  # feature selection
    parser.add_argument("-l", "--label-method", type=str, default="aha", help=label_methods_desc)
    args = parser.parse_args()

    # setup testing parameters
    n_jobs = args.jobs
    if n_jobs == -1 or n_jobs > mp.cpu_count():
        n_jobs = (mp.cpu_count() - 1) if mp.cpu_count() > 1 else 1

    print(args)
    n_test_iters = args.iter
    n_cv_folds = args.cv_fold
    test_size = args.test_percent / 100
    if test_size > 1:
        test_size = 0.3

    class_weight = None
    if args.balanced_weight:
        class_weight = "balanced"

    selected_features = args.features

    # build scoring function
    if args.scorer == "ber":  # BER-based scoring function
        cv_scorer = metrics.make_scorer(balanced_error_rate, greater_is_better=False)
    else:
        cv_scorer = args.scorer
        # cv_scorer = metrics.make_scorer(metrics.fbeta_score, beta=10.0)

    # load features
    x_data, x_data_info = load_features(args.input)
    # only select the specified feature
    if selected_features:
        x_data = x_data[:, selected_features]

    # encode differnt types of rhythm names into numeric codes for stratified sampling later
    y_rhythm_names = [info.rhythm for info in x_data_info]
    label_encoder = preprocessing.LabelEncoder()
    y_rhythm_types = label_encoder.fit_transform(y_rhythm_names)

    # label the samples
    y_data = make_labels(x_data_info, args.label_method)

    # build estimators to test
    estimator_name = args.model
    estimator = None
    param_grid = None
    support_class_weight = False
    if estimator_name == "logistic_regression":
        from sklearn import linear_model
        estimator = linear_model.LogisticRegression(class_weight=class_weight)
        param_grid = {
            "C": np.logspace(-4, 4, 10)
        }
        support_class_weight = True
    elif estimator_name == "random_forest":
        estimator = ensemble.RandomForestClassifier(class_weight=class_weight)
        param_grid = {
            "n_estimators": list(range(10, 110, 10))
        }
        support_class_weight = True
    elif estimator_name == "gradient_boosting":
        estimator = ensemble.GradientBoostingClassifier(learning_rate=0.1)
        param_grid = {
            "n_estimators": list(range(150, 250, 10)),
            "max_depth": list(range(3, 8))
        }
    elif estimator_name == "adaboost":
        estimator = ensemble.AdaBoostClassifier()
        param_grid = {
            "n_estimators": list(range(30, 150, 10)),
            "learning_rate": np.logspace(-1, 0, 2)
        }
    elif estimator_name == "svc":
        from sklearn import svm
        estimator = svm.SVC(shrinking=False,
                            cache_size=2048,
                            verbose=False,
                            probability=True,
                            class_weight=class_weight)
        param_grid = {
            "C": np.logspace(0, 1, 2),
            "gamma": np.logspace(-2, -1, 2)
        }
        support_class_weight = True
    elif estimator_name == "mlp1" or estimator_name == "mlp2":  # multiple layer perceptron neural network
        from sknn import mlp
        param_grid = {
            "learning_rate": [0.0001],
            "regularize": ["l2"],  # , "dropout"],
            "weight_decay": np.logspace(-6, -5, 2),  # parameter for L2 regularizer
            "hidden0__type": ["Tanh"]  # "Rectifier", "Sigmoid"
        }

        layers = [mlp.Layer(type="Tanh", name="hidden0")]
        # add the second hidden layer as needed
        if estimator_name == "mlp2":  # 2 hidden layer
            layers.append(mlp.Layer(type="Tanh", name="hidden1"))
            param_grid["hidden0__units"] = list(range(2, 5, 1))
            param_grid["hidden1__units"] = list(range(2, 5, 1))
            param_grid["hidden1__type"] = ["Tanh"]  # "Rectifier", "Sigmoid"
        else:
            param_grid["hidden0__units"] = list(range(5, 26, 1))
        # add the output layer
        layers.append(mlp.Layer("Softmax"))
        estimator = mlp.Classifier(layers=layers, batch_size=150)

    # Run the selected test
    if args.label_method == "aha":
        _csv_fields = ["TPR", "TNR", "PPV"]
        csv_fields = ["iter"]
        for class_name in (aha_classe_names + list(label_encoder.classes_)):
            csv_fields.extend(["{0}[{1}]".format(field, class_name) for field in _csv_fields])
    else:
        csv_fields = ["iter", "Se", "Sp", "PPV", "Acc", "Se(Sp95)", "Se(Sp97)", "Se(Sp99)", "TP", "TN", "FP", "FN"]

    # add tuned optimal parameters to the csv file
    csv_fields.extend(sorted(param_grid.keys()))
    with open(args.output, "w", newline="", buffering=1) as f:  # buffering=1 means line buffering
        rows = []
        writer = csv.DictWriter(f, fieldnames=csv_fields)
        writer.writeheader()
        # perform the test for many times
        for it in range(n_test_iters):
            print(estimator_name, it)
            row = {"iter" : it}
            # Here we split the indicies of the rows rather than the data array itself.
            x_indicies = list(range(len(x_data)))
            x_train_idx, x_test_idx, y_train, y_test = cross_validation.train_test_split(x_indicies,
                                                                                         y_data,
                                                                                         test_size=test_size,
                                                                                         stratify=y_data)
            x_train = x_data[x_train_idx]
            x_test = x_data[x_test_idx]
            x_test_info = x_data_info[x_test_idx]

            # scale the features (NOTE: training and testing sets should be scaled separately.)
            preprocessing.scale(x_train, copy=False)
            preprocessing.scale(x_test, copy=False)

            fit_params = None
            # try to balance class weighting
            if args.balanced_weight and not support_class_weight and not (args.label_method == "aha" or args.label_method == "3"):
                # perform sample weighting instead if the estimator does not support class weighting
                weight_arg = "w" if estimator_name.startswith("mlp") else "sample_weight"
                fit_params = {
                    weight_arg: np.array(get_sample_weights(y_train))
                }

            grid = grid_search.GridSearchCV(estimator,
                                            param_grid,
                                            fit_params=fit_params,
                                            scoring=cv_scorer,
                                            n_jobs=n_jobs,
                                            cv=n_cv_folds,
                                            verbose=0)

            # perform the classification test
            grid.fit(x_train, y_train)
            y_predict = grid.predict(x_test)
            if estimator_name.startswith("mlp"):  # sknn has different format of output and it needs to be flatten into a 1d array.
                y_predict = y_predict.flatten()

            if args.label_method == "aha" or args.label_method == "3":  # multi-class for AHA clasification scheme
                results = MultiClassificationResult(y_test, y_predict, classes=aha_classes).results
                for class_name, result in zip(aha_classe_names, results):
                    row["TPR[{0}]".format(class_name)] = result.sensitivity
                    row["TNR[{0}]".format(class_name)] = result.specificity
                    row["PPV[{0}]".format(class_name)] = result.precision

                # report for each rhythm type (FIXME: is this correct?)
                for rhythm_id, rhythm_name in enumerate(label_encoder.classes_):
                    y_test_rhythm_types = y_rhythm_types[x_test_idx]
                    idx = (y_test_rhythm_types == rhythm_id)
                    result = BinaryClassificationResult(y_test[idx], y_predict[idx])
                    row["TPR[{0}]".format(rhythm_name)] = result.sensitivity
                    row["TNR[{0}]".format(rhythm_name)] = result.specificity
                    row["PPV[{0}]".format(rhythm_name)] = result.precision
            else:  # simple binary classification
                result = BinaryClassificationResult(y_test, y_predict)
                row["Se"] = result.sensitivity
                row["Sp"] = result.specificity
                row["PPV"] = result.precision
                row["Acc"] = result.accuracy
                row["TP"] = result.tp
                row["TN"] = result.tn
                row["FP"] = result.fp
                row["FN"] = result.fn

                # prediction with probabilities
                if hasattr(estimator, "predict_proba"):
                    y_predict_scores = grid.predict_proba(x_test)[:, 1]
                    false_pos_rate, true_pos_rate, thresholds = metrics.roc_curve(y_test, y_predict_scores, pos_label=1)
                    # find sensitivity at 95% specificity
                    x = np.searchsorted(false_pos_rate, 0.05)
                    row["Se(Sp95)"] = true_pos_rate[x]

                    x = np.searchsorted(false_pos_rate, 0.03)
                    row["Se(Sp97)"] = true_pos_rate[x]

                    x = np.searchsorted(false_pos_rate, 0.01)
                    row["Se(Sp99)"] = true_pos_rate[x]

            # best parameters of grid search
            row.update(grid.best_params_)
            rows.append(row)  # remember each row so we can calculate average for them later

            print("  ", ", ".join(["{0}={1:.3f}".format(field, row.get(field, "")) for field in csv_fields]))
            writer.writerow(row)

        # calculate average for all iterations automatically and write to csv
        n_params = len(param_grid)
        fields = csv_fields[1:-n_params]
        avg = {"iter": "average"}
        for field in fields:
            col = [row[field] for row in rows]
            avg[field] = np.mean(col)
        writer.writerow(avg)


if __name__ == "__main__":
    main()
