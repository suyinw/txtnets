__author__ = 'mdenil'


import numpy as np
import os
import time
import random
import simplejson as json
import cPickle as pickle
from nltk.tokenize import WordPunctTokenizer

from gpu.model.model import CSM
from gpu.model.encoding import DictionaryEncoding
from gpu.model.embedding import WordEmbedding
from gpu.model.transfer import SentenceConvolution
from gpu.model.transfer import Bias
from gpu.model.pooling import SumFolding
from gpu.model.pooling import MaxFolding
from gpu.model.pooling import KMaxPooling
from gpu.model.nonlinearity import Tanh
from gpu.model.transfer import Softmax
from gpu.model.transfer import Linear
from gpu.model.dropout import Dropout

from gpu.model.cost import CrossEntropy

from gpu.optimize.sgd import SGD
from gpu.optimize.objective import CostMinimizationObjective
from gpu.optimize.regularizer import L2Regularizer
from gpu.optimize.update_rule import AdaGrad
from gpu.optimize.update_rule import AdaDelta
from gpu.optimize.data_provider import LabelledSequenceMinibatchProvider

from cpu.optimize.grad_check import ModelGradientChecker

import gpu.model.dropout

def run():
    random.seed(435)
    np.random.seed(2342)
    np.set_printoptions(linewidth=100)

    tweets_dir = os.path.join("../data", "sentiment140_2")  # _2 truncates at <3, normal truncates at <5

    with open(os.path.join(tweets_dir, "sentiment140.train.clean.json")) as data_file:
        data = json.loads(data_file.read())
        random.shuffle(data)
        X, Y = map(list, zip(*data))
        Y = [[":)", ":("].index(y) for y in Y]

    with open(os.path.join(tweets_dir, "sentiment140.train.clean.dictionary.encoding.json")) as alphabet_file:
        alphabet = json.loads(alphabet_file.read())

    with open(os.path.join(tweets_dir, "sentiment140.test.clean.json")) as data_file:
        data = json.loads(data_file.read())
        X_test, Y_test = map(list, zip(*data))
        Y_test = [[":)", ":("].index(y) for y in Y_test]

    print len(alphabet)

    # X = X[:1000]
    # Y = Y[:1000]

    # lists of words
    # replace unknowns with an unknown character
    tokenizer = WordPunctTokenizer()
    new_X = []
    for x in X:
        new_X.append([w if w in alphabet else 'UNKNOWN' for w in tokenizer.tokenize(x)])
    X = new_X

    new_X_test = []
    for x in X_test:
        new_X_test.append([w if w in alphabet else 'UNKNOWN' for w in tokenizer.tokenize(x)])
    X_test = new_X_test


    batch_size = 25

    train_data_provider = LabelledSequenceMinibatchProvider(
        X=X,
        Y=Y,
        batch_size=batch_size,
        fixed_length=50,
        padding='PADDING')

    print train_data_provider.batches_per_epoch

    validation_data_provider = LabelledSequenceMinibatchProvider(
        X=X_test,
        Y=Y_test,
        batch_size=len(X_test),
        fixed_length=50,
        padding='PADDING',
        shuffle=False)


    tweet_model = CSM(
        layers=[
            DictionaryEncoding(vocabulary=alphabet),

            WordEmbedding(
                dimension=60,
                vocabulary_size=len(alphabet),
                padding=alphabet['PADDING']),

            Dropout(('b', 'f'), 0.5),

            SentenceConvolution(
                n_feature_maps=6,
                kernel_width=7,
                n_channels=60,
                n_input_dimensions=1),

            Bias(
                n_input_dims=1,
                n_feature_maps=6),

            KMaxPooling(k=4, k_dynamic=0.5),

            Tanh(),

            Dropout(('b', 'd', 'f'), 0.5),


            SentenceConvolution(
                n_feature_maps=14,
                kernel_width=5,
                n_channels=6,
                n_input_dimensions=1),

            Bias(
                n_input_dims=1,
                n_feature_maps=14),

            KMaxPooling(k=4),

            Tanh(),

            Dropout(('b', 'd', 'f', 'w'), 0.5),

            # Linear(n_input=4*14, n_output=4*14),
            #
            # Bias(
            #     n_input_dims=4*14,
            #     n_feature_maps=1),
            #
            # Dropout(('b', 'd', 'f', 'w'), 0.5),

            Softmax(
                n_classes=2,
                n_input_dimensions=14*4),
            ]
    )

    print tweet_model

    cost_function = CrossEntropy()

    regularizer = L2Regularizer(lamb=1e-5)

    objective = CostMinimizationObjective(
        cost=cost_function,
        data_provider=train_data_provider,
        regularizer=regularizer)

    gradient_checker = ModelGradientChecker(
        CostMinimizationObjective(
            cost=cost_function,
            data_provider=validation_data_provider,
            regularizer=regularizer))

    time_start = time.time()

    best_acc = -1.0

    costs = []
    def numbers():
        i = 0
        while True:
            yield i
            i += 1
    batch_index = 0
    for _ in numbers():
        update_rule = AdaGrad(
            gamma=0.1,
            model_template=tweet_model)

        optimizer = SGD(
            model=tweet_model,
            objective=objective,
            update_rule=update_rule)

        for iteration_info in iter(optimizer):
            batch_index += 1
            costs.append(iteration_info['cost'])

            if batch_index % 30 == 0:
                X_valid, Y_valid, meta_valid = validation_data_provider.next_batch()

                test_model = gpu.model.dropout.remove_dropout(tweet_model)
                Y_hat = test_model.fprop(X_valid, meta=meta_valid)
                del test_model

                Y_hat = Y_hat.get()
                assert np.all(np.abs(Y_hat.sum(axis=1) - 1) < 1e-6)

                # grad_check = gradient_checker.check(model)
                grad_check = "skipped"

                time_now = time.time()
                examples_per_hr = (batch_index * batch_size) / (time_now - time_start) * 3600

                acc = np.mean(np.argmax(Y_hat, axis=1) == np.argmax(Y_valid.get(), axis=1))

                if acc > best_acc:
                    best_acc = acc
                    with open("model_best_tweets_LASTCHANCE_words.pkl", 'w') as model_file:
                        pickle.dump(tweet_model.move_to_cpu(), model_file, protocol=-1)
                    # with open("model_best_optimization.pkl", 'w') as model_file:
                    #     pickle.dump(optimizer, model_file, protocol=-1)


                print "B: {}, A: {}, C: {}, Prop1: {}, Param size: {}, EPH: {}, best acc: {}".format(
                    batch_index,
                    acc,
                    costs[-1],
                    np.argmax(Y_hat, axis=1).mean(),
                    np.mean(np.abs(tweet_model.pack())),
                    examples_per_hr,
                    best_acc)

            if batch_index % 32000 == 0:
                break

    time_end = time.time()

    print "Time elapsed: {}s".format(time_end - time_start)


if __name__ == "__main__":
    run()