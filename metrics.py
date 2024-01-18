
import warnings

from sklearn_extra.cluster import KMedoids
from sklearn.metrics import homogeneity_score
import scipy
import sklearn
#import tensorflow as tf
import torch
import numpy as np

from scipy.special import softmax
from sklearn.feature_selection import mutual_info_classif
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.neural_network import MLPClassifier
from tqdm import tqdm

# Pytorch reimplementation of CEM metrics from: https://github.com/mateoespinosa/cem
# The code is almost verbatim, but Tensorflow-related functions are replaced with Pytorch ones.


########################################

# cem.metrics.oracle

########################################

'''
Efficient implementation of the Oracle Impurity Score (OIS)
taken from https://github.com/mateoespinosa/concept-quality.

All credit goes to Espinosa Zarlenga et al. AAAI 2023.

Metrics to measure concept purity inspired on the definition of purity by
Mahinpei et al.'s "Promises and Pitfalls of Black-Box Concept Learning Models"
(found at https://arxiv.org/abs/2106.13314).
'''




################################################################################
## Concept Whitening Concept Purity Metrics
################################################################################

def concept_similarity_matrix(
    concept_representations,
    compute_ratios=False,
    eps=1e-5,
):
    """
    Computes a matrix such that its (i,j)-th entry represents the average
    normalized dot product between samples representative of concept i and
    samples representative of concept j.
    This metric is defined by Chen et al. in "Concept Whitening for
    Interpretable Image Recognition" (https://arxiv.org/abs/2002.01650)

    :param List[np.ndarray] concept_representations: A list of tensors
        containing representative samples for each concept. The i-th element
        of this list must be a tensor whose first dimension is the batch
        dimension and last dimension is the channel dimension.
    :param bool compute_ratios: If True, then each element in the output matrix
        is  the similarity ratio coefficient as defined by Chen et al.. This is
        the ratio between the inter-similarity of (i, j) and the square root
        of the product between the intra-similarity of concepts i and j.
    :param float eps: A small value for numerical stability when performing
        divisions.
    """
    num_concepts = len(concept_representations)
    result = np.zeros((num_concepts, num_concepts), dtype=np.float32)
    m_representations_normed = {}
    intra_dot_product_means_normed = {}
    for i in range(num_concepts):
        m_representations_normed[i] = (
            concept_representations[i] /
            np.linalg.norm(concept_representations[i], axis=-1, keepdims=True)

        )
        intra_dot_product_means_normed[i] = np.matmul(
            m_representations_normed[i],
            m_representations_normed[i].transpose()
        ).mean()

        if compute_ratios:
            result[i, i] = 1.0
        else:
            result = np.matmul(
                concept_representations[i],
                concept_representations[i].transpose()
            ).mean()

    for i in range(num_concepts):
        for j in range(i + 1, num_concepts):
            inter_dot = np.matmul(
                m_representations_normed[i],
                m_representations_normed[j].transpose()
            ).mean()
            if compute_ratios:
                result[i, j] = np.abs(inter_dot) / np.sqrt(np.abs(
                    intra_dot_product_means_normed[i] *
                    intra_dot_product_means_normed[j]
                ))
            else:
                result[i, j] = np.matmul(
                    concept_representations[i],
                    concept_representations[j].transpose(),
                ).mean()
            result[j, i] = result[i, j]

    return result


################################################################################
## Alignment Functions
################################################################################


def find_max_alignment(matrix):
    """
    Finds the maximum (greedy) alignment between columns in this matrix and
    its rows. It returns a list `l` with as many elements as columns in the input
    matrix such that l[i] is the column best aligned with row `i` given the
    scores in `matrix`.
    For this, we proceed in a greedy fashion where we bind columns with rows
    in descending order of their values in the matrix.

    :param np.ndarray matrix: A matrix with at least as many rows as columns.

    :return List[int]: the column-to-row maximum greedy alignment.
    """
    sorted_inds = np.dstack(
        np.unravel_index(np.argsort(-matrix.ravel()), matrix.shape)
    )[0]
    result_alignment = [None for _ in range(matrix.shape[1])]
    used_rows = set()
    used_cols = set()
    for (row, col) in sorted_inds:
        if (col in used_cols) or (row in used_rows):
            # Then this is not something we can use any more
            continue
        # Else, let's add this mapping into our alignment!
        result_alignment[col] = row
        used_rows.add(row)
        used_cols.add(col)
        if len(used_rows) == matrix.shape[1]:
            # Then we are done in here!
            break
    return result_alignment


def max_alignment_matrix(matrix):
    """
    Helper function that computes the (greedy) max alignment of the input
    matrix and it rearranges so that each column is aligned to its corresponding
    row. In this case, this means that the diagonal matrix of the resulting
    matrix will correspond to the entries in `matrix` that were aligned.

    :param np.ndarray matrix: A matrix with at least as many rows as columns.

    :return np.ndarray: A square matrix representing the column-aligned matrix
        of the given input tensor.
    """
    inds = find_max_alignment(matrix)
    return np.stack(
        [matrix[inds[i], :] for i in range(matrix.shape[1])],
        axis=0
    )


################################################################################
## Purity Matrix Computation
################################################################################


def concept_purity_matrix(
    c_soft,
    c_true,
    concept_label_cardinality=None,
    predictor_model_fn=None,
    predictor_train_kwags=None,
    test_size=0.2,
    ignore_diags=False,
    jointly_learnt=False,
):
    """
    Computes a concept purity matrix where the (i,j)-th entry represents the
    predictive accuracy of a classifier trained to use the i-th concept's soft
    labels (as given by c_soft_train) to predict the ground truth value of the
    j-th concept.

    This process is informally defined only for binary concepts by Mahinpei et
    al.'s in "Promises and Pitfalls of Black-Box Concept Learning Models".
    Nevertheless, this method supports both binary concepts (given as a 2D
    matrix in c_soft) or categorical concepts (given by a list of 2D matrices
    in argument c_soft).

    :param Or[np.ndarray, List[np.ndarray]] c_soft: Predicted set of "soft"
        concept representations by a concept encoder model applied to the
        testing data. This argument must be an np.ndarray with shape
        (n_samples, ..., n_concepts) where the concept representation may be
        of any rank as long as the last dimension is the dimension used to
        separate distinct concept representations. If concepts have distinct
        array shapes for their representations, then this argument is expected
        to be a list of `n_concepts` np.ndarrays where the i-th element in the
        list is an array with shape (n_samples, ...) containing the tensor
        representation of the i-th concept.
        Note that in either case we only require that the first dimension.
    :param np.ndarray c_true: Ground truth concept values in one-to-one
        correspondence with concepts in c_soft. Shape must be
        (n_samples, n_concepts).
    :param List[int] concept_label_cardinality: If given, then this is a list
        of integers such that its i-th index contains the number of classes
        that the it-th concept may take. If not given, then we will assume that
        all concepts have the same cardinality as the number of activations in
        their soft representations.
    :param Function[(int, int), sklearn-like Estimator] predictor_model_fn: A
        function generator that takes as an argument two values, the number of
        classes for the input concept and the number of classes for the output
        target concept, respectively, and produces an sklearn-like Estimator
        which one can train for predicting a concept given another concept's
        soft concept values. If not given then we will use a 3-layer ReLU MLP
        as our predictor.
    :param Dict[Any, Any] predictor_train_kwags: optional arguments to pass
        the estimator being when calling its `fit` method.
    :param float test_size: A value in [0, 1] indicating the fraction of the
        given data that will be used to evaluate the trained concept-based
        classifier. The rest of the data will be used for training said
        classifier.

    :return np.ndarray: a matrix with shape (n_concepts, n_concepts)
        where the (i,j)-th entry specifies the testing AUC of using the i-th
        concept soft representations to predict the j-th concept.
    """
    # Start by handling default arguments
    predictor_train_kwags = predictor_train_kwags or {}

    # Check that their rank is the expected one
    assert len(c_true.shape) == 2, (
        f'Expected testing concept predictions to be a matrix with shape '
        f'(n_samples, n_concepts) but instead got a matrix with shape '
        f'{c_true.shape}'
    )

    # Construct a list concept_label_cardinality that maps a concept to the
    # cardinality of its label set as specified by the testing data
    (n_samples, n_true_concepts) = c_true.shape
    if isinstance(c_soft, np.ndarray):
        n_soft_concepts = c_soft.shape[-1]
    else:
        assert isinstance(c_soft, list), (
            f'c_soft must be passed as either a list or a np.ndarray. '
            f'Instead we got an instance of "{type(c_soft).__name__}".'
        )
        n_soft_concepts = len(c_soft)

    assert n_soft_concepts >= n_true_concepts, (
        f'Expected at least as many soft concept representations as true '
        f'concepts labels. However we received {n_soft_concepts} soft concept '
        f'representations per sample while we have {n_true_concepts} true '
        f'concept labels per sample.'
    )

    if isinstance(c_soft, np.ndarray):
        # Then, all concepts must have the same representation size
        assert c_soft.shape[0] == c_true.shape[0], (
            f'Expected a many test soft-concepts as ground truth test '
            f'concepts. Instead got {c_soft.shape[0]} soft-concepts '
            f'and {c_true.shape[0]} ground truth test concepts.'
        )
        if concept_label_cardinality is None:
            concept_label_cardinality = [2 for _ in range(n_soft_concepts)]
        # And for simplicity and consistency, we will rewrite c_soft as a
        # list such that i-th entry contains an array with shape
        # (n_samples, repr_size) indicating the representation of the i-th
        # concept for all samples
        new_c_soft = [None for _ in range(n_soft_concepts)]
        for i in range(n_soft_concepts):
            if len(c_soft.shape) == 1:
                # If it is a scalar representation, then let's make it explicit
                new_c_soft[i] = np.expand_dims(c_soft[..., i], axis=-1)
            else:
                new_c_soft[i] = c_soft[..., i]
        c_soft = new_c_soft
    else:
        # Else, time to infer these values from the given list of soft
        # labels
        assert isinstance(c_soft, list), (
            f'c_soft must be passed as either a list or a np.ndarray. '
            f'Instead we got an instance of "{type(c_soft).__name__}".'
        )
        if concept_label_cardinality is None:
            concept_label_cardinality = [None for _ in range(n_soft_concepts)]
            for i, soft_labels in enumerate(c_soft):
                concept_label_cardinality[i] = max(soft_labels.shape[-1], 2)
                assert soft_labels.shape[0] == c_true.shape[0], (
                    f"For concept {i}'s soft labels, we expected "
                    f"{c_true.shape[0]} samples as we were given that many "
                    f"in the ground-truth array. Instead we found "
                    f"{soft_labels.shape[0]} samples."
                )

    # Handle the default parameters for both the generating function and
    # the concept label cardinality
    if predictor_model_fn is None:
        # Then by default we will use a simple MLP classifier with one hidden
        # ReLU layer with 32 units in it
        def predictor_model_fn(
            output_concept_classes=2,
        ):
            # estimator = tf.keras.models.Sequential([
            #     tf.keras.layers.Dense(
            #         32,
            #         activation='relu',
            #         name="predictor_fc_1",
            #     ),
            #     tf.keras.layers.Dense(
            #         output_concept_classes if output_concept_classes > 2 else 1,
            #         # We will merge the activation into the loss for numerical
            #         # stability
            #         activation=None,
            #         name="predictor_fc_out",
            #     ),
            # ])

            estimator = torch.nn.Sequential(
                torch.nn.LazyLinear(32),
                torch.nn.ReLU(),
                torch.nn.Linear(32, output_concept_classes if output_concept_classes > 2 else 1)
            )


            # if jointly_learnt:
            #     loss = tf.nn.sigmoid_cross_entropy_with_logits
            # else:
            #     loss = (
            #         tf.keras.losses.SparseCategoricalCrossentropy(
            #             from_logits=True
            #         ) if output_concept_classes > 2 else
            #         tf.keras.losses.BinaryCrossentropy(
            #             from_logits=True,
            #         )
            #     )
            if jointly_learnt:
                loss = torch.nn.functional.binary_cross_entropy_with_logits
            else:
                loss = (
                        torch.nn.functional.cross_entropy if output_concept_classes > 2 else
                    torch.nn.functional.binary_cross_entropy_with_logits
                )
            estimator.compile(
                # Use ADAM optimizer by default
                optimizer='adam',
                # Note: we assume labels come without a one-hot-encoding in the
                #       case when the concepts are categorical.
                loss=loss,
            )
            return estimator

    predictor_train_kwags = predictor_train_kwags or {
        'epochs': 25,
        'batch_size': min(512, n_samples),
        'verbose': 0,
    }

    # Time to start formulating our resulting matrix
    result = np.zeros((n_soft_concepts, n_true_concepts), dtype=np.float32)

    # Split our test data into two subsets as we will need to train
    # a classifier and then use that trained classifier in the remainder of the
    # data for computing our scores
    train_indexes, test_indexes = train_test_split(
        list(range(n_samples)),
        test_size=test_size,
    )

    for src_soft_concept in tqdm(range(n_soft_concepts)):

        # Construct a test and training set of features for this concept
        concept_soft_train_x = c_soft[src_soft_concept][train_indexes, ...]
        concept_soft_test_x = c_soft[src_soft_concept][test_indexes, ...]
        if len(concept_soft_train_x.shape) == 1:
            concept_soft_train_x = concept_soft_train_x[:,None]
            concept_soft_test_x = concept_soft_test_x[:, None]
        if jointly_learnt:
            # Construct a new estimator for performing this prediction
            output_size = 0
            for tgt_true_concept in range(n_true_concepts):
                output_size += (
                    concept_label_cardinality[tgt_true_concept]
                    if concept_label_cardinality[tgt_true_concept] > 2
                    else 1
                )
            estimator = predictor_model_fn(output_size)
            # Train it
            estimator.fit(
                concept_soft_train_x,
                c_true[train_indexes, :],
                **predictor_train_kwags,
            )
            # Compute the AUC of this classifier on the test data
            preds = estimator.predict(concept_soft_test_x)
            for tgt_true_concept in range(n_true_concepts):
                true_concepts = c_true[test_indexes, tgt_true_concept]
                used_preds = preds[:, tgt_true_concept]
                if concept_label_cardinality[tgt_true_concept] > 2:
                    # Then lets apply a softmax activation over all the probability
                    # classes
                    used_preds = scipy.special.softmax(used_preds, axis=-1)

                    # And make sure we only compute the AUC of labels that are
                    # actually used
                    used_labels = np.sort(np.unique(true_concepts))

                    # And select just the labels that are in fact being used
                    true_concepts = torch.nn.functional.one_hot(
                        true_concepts,
                        num_classes=concept_label_cardinality[tgt_true_concept],
                    )[:, used_labels]
                    used_preds = used_preds[:, used_labels]
                if len(np.unique(true_concepts)) > 1:
                    auc = sklearn.metrics.roc_auc_score(
                        true_concepts,
                        used_preds,
                        multi_class='ovo',
                    )
                else:
                    if concept_label_cardinality[tgt_true_concept] <= 2:
                        used_preds = (
                            scipy.special.expit(used_preds) >= 0.5
                        ).astype(np.int32)
                    else:
                        used_preds = np.argmax(used_preds, axis=-1)
                        true_concepts = np.argmax(true_concepts, axis=-1)
                    auc = sklearn.metrics.accuracy_score(
                        true_concepts,
                        used_preds,
                    )

                # Finally, time to populate the actual entry of our resulting
                # matrix
                result[src_soft_concept, tgt_true_concept] = auc
        else:
            for tgt_true_concept in range(n_true_concepts):
                # Let's populate the (i,j)-th entry of our matrix by first
                # training a classifier to predict the ground truth value of
                # concept j using the soft-concept labels for concept i.
                if ignore_diags and (src_soft_concept == tgt_true_concept):
                    # Then for simplicity sake we will simply set this to one
                    # as it is expected to be perfectly predictable
                    result[src_soft_concept, tgt_true_concept] = 1
                    continue

                # Construct a new estimator for performing this prediction
                estimator = predictor_model_fn(
                    concept_label_cardinality[tgt_true_concept]
                )
                # Train it
                estimator.fit(
                    concept_soft_train_x,
                    c_true[train_indexes, tgt_true_concept:(tgt_true_concept + 1)],
                    **predictor_train_kwags,
                )

                # Compute the AUC of this classifier on the test data
                preds = estimator.predict(concept_soft_test_x)
                true_concepts = c_true[test_indexes, tgt_true_concept]
                if concept_label_cardinality[tgt_true_concept] > 2:
                    # Then lets apply a softmax activation over all the
                    # probability classes
                    preds = scipy.special.softmax(preds, axis=-1)

                    # And make sure we only compute the AUC of labels that are
                    # actually used
                    used_labels = np.sort(np.unique(true_concepts))

                    # And select just the labels that are in fact being used
                    true_concepts = torch.nn.functional.one_hot(true_concepts,
                                                                num_classes=concept_label_cardinality[tgt_true_concept]
                                                                )[:, used_labels]
                    preds = preds[:, used_labels]

                auc = sklearn.metrics.roc_auc_score(
                    true_concepts,
                    preds,
                    multi_class='ovo',
                )

                # Finally, time to populate the actual entry of our resulting
                # matrix
                result[src_soft_concept, tgt_true_concept] = auc

    # And that's all folks
    return result


def encoder_concept_purity_matrix(
    encoder_model,
    features,
    concepts,
    predictor_model_fn=None,
    predictor_train_kwags=None,
    test_size=0.2,
    jointly_learnt=False,
):
    """
    Computes a concept purity matrix where the (i,j)-th entry represents the
    predictive accuracy of a classifier trained to use the i-th concept's soft
    representation (as given by the encoder model) to predict the ground truth
    value of the j-th concept.

    This process is informally defined only for binary concepts by Mahinpei et
    al.'s in "Promises and Pitfalls of Black-Box Concept Learning Models".
    Nevertheless, this method supports arbitrarily-shaped concept
    representations (given as a (n_samples, ..., n_concepts) tensor output when
    using the encoder's predict method) as well as concepts with different
    representation shapes (given as a list of n_concepts  tensors with shapes
    (n_samples, ...) when using the encoder's predict method).

    :param skelearn-like Estimator encoder_model: An encoder estimator capable
        of extracting concept representations from a set of features. For
        example, this estimator may produce a vector of binary concept
        probabilities for each sample (i.e., in the case of all concepts being
        binary) or a list of vectors representing probability distributions over
        the labels for each concept (i.e., in the case of one or more concepts
        being categorical).
    :param np.ndarray features: An array of testing samples with shape
        (n_samples, ...) used to compute the purity matrix.
    :param np.ndarray concepts: Ground truth concept values in one-to-one
        correspondence with samples in features. Shape must be
        (n_samples, n_concepts).
    :param Function[(int,), sklearn-like Estimator] predictor_model_fn: A
        function generator that takes as an argument the number of
        the output target concept and produces an sklearn-like Estimator
        which one can train for predicting a concept given another concept's
        soft concept values. If not given then we will use a 3-layer ReLU MLP
        as our predictor.
    :param Dict[Any, Any] predictor_train_kwags: optional arguments to pass
        the estimator when calling its `fit` method.
    :param float test_size: A value in [0, 1] indicating the fraction of the
        given data that will be used to evaluate the trained concept-based
        classifier. The rest of the data will be used for training said
        classifier.

    :return np.ndarray: a matrix with shape (n_concepts, n_concepts)
        where the (i,j)-th entry specifies the testing AUC of using the i-th
        concept soft labels to predict the j-th concept.
    """
    # Simply use the concept purity matrix computation defined above when given
    # soft concepts as computed by the encoder model
    return concept_purity_matrix(
        c_soft=encoder_model.predict(features),
        c_true=concepts,
        predictor_model_fn=predictor_model_fn,
        predictor_train_kwags=predictor_train_kwags,
        test_size=test_size,
        jointly_learnt=jointly_learnt,
    )


def oracle_purity_matrix(
    concepts,
    concept_label_cardinality=None,
    predictor_model_fn=None,
    predictor_train_kwags=None,
    test_size=0.2,
    jointly_learnt=False,
):
    """
    Computes an oracle's concept purity matrix where the (i,j)-th entry
    represents the predictive accuracy of a classifier trained to use the i-th
    concept (ground truth) to predict the ground truth value of the j-th
    concept.

    :param np.ndarray concepts: Ground truth concept values. Shape must be
        (n_samples, n_concepts).
    :param List[int] concept_label_cardinality: If given, then this is a list
        of integers such that its i-th index contains the number of classes
        that the it-th concept may take. If not given, then we will assume that
        all concepts are binary (i.e., concept_label_cardinality[i] = 2 for all
        i).
    :param Function[(int,), sklearn-like Estimator] predictor_model_fn: A
        function generator that takes as an argument the number of
        the output target concept and produces an sklearn-like Estimator
        which one can train for predicting a concept given another concept's
        soft concept values. If not given then we will use a 3-layer ReLU MLP
        as our predictor.
    :param Dict[Any, Any] predictor_train_kwags: optional arguments to pass
        the estimator when calling its `fit` method.
    :param float test_size: A value in [0, 1] indicating the fraction of the
        given data that will be used to evaluate the trained concept-based
        classifier. The rest of the data will be used for training said
        classifier.

    :return np.ndarray: a matrix with shape (n_concepts, n_concepts)
        where the (i,j)-th entry specifies the testing AUC of using the i-th
        concept label to predict the j-th concept.
    """

    return concept_purity_matrix(
        c_soft=concepts,
        c_true=concepts,
        concept_label_cardinality=concept_label_cardinality,
        predictor_model_fn=predictor_model_fn,
        predictor_train_kwags=predictor_train_kwags,
        test_size=test_size,
        ignore_diags=True,
        jointly_learnt=jointly_learnt,
    )


################################################################################
## Purity Metrics
################################################################################

def normalize_impurity(impurity, n_concepts):
    return impurity / (n_concepts / 2)


def oracle_impurity_score(
    c_soft,
    c_true,
    predictor_model_fn=None,
    predictor_train_kwags=None,
    test_size=0.2,
    norm_fn=lambda x: np.linalg.norm(x, ord='fro'),
    oracle_matrix=None,
    purity_matrix=None,
    output_matrices=False,
    alignment_function=None,
    concept_label_cardinality=None,
    jointly_learnt=False,
    include_diagonal=True,
):
    """
    Returns the oracle impurity score (OIS) of the given soft concept
    representations `c_soft` with respect to their corresponding ground truth
    concepts `c_true`. This value is higher if concepts encode unnecessary
    information from other concepts in their soft representation and lower
    otherwise. If zero, then all soft concept labels are considered to be
    "pure".

    We compute this metric by calculating the norm of the absolute difference
    between the purity matrix derived from the soft concepts and the purity
    matrix derived from an oracle model. This oracle model is trained using
    the ground truth labels instead of the soft labels and may capture trivial
    relationships between different concept labels.

    :param Or[np.ndarray, List[np.ndarray]] c_soft: Predicted set of "soft"
        concept representations by a concept encoder model applied to the
        testing data. This argument must be an np.ndarray with shape
        (n_samples, ..., n_concepts) where the concept representation may be
        of any rank as long as the last dimension is the dimension used to
        separate distinct concept representations. If concepts have distinct
        array shapes for their representations, then this argument is expected
        to be a list of `n_concepts` np.ndarrays where the i-th element in the
        list is an array with shape (n_samples, ...) containing the tensor
        representation of the i-th concept.
        Note that in either case we only require that the first dimension.
    :param np.ndarray c_true: Ground truth concept values in one-to-one
        correspondence with concepts in c_soft. Shape must be
        (n_samples, n_concepts).
    :param Function[(int,), sklearn-like Estimator] predictor_model_fn: A
        function generator that takes as an argument the number of
        the output target concept and produces an sklearn-like Estimator
        which one can train for predicting a concept given another concept's
        soft concept values. If not given then we will use a 3-layer ReLU MLP
        as our predictor.
    :param Dict[Any, Any] predictor_train_kwags: optional arguments to pass
        the estimator being when calling its `fit` method.
    :param float test_size: A value in [0, 1] indicating the fraction of the
        given data that will be used to evaluate the trained concept-based
        classifier. The rest of the data will be used for training said
        classifier.
    :param Function[(np.ndarray), float] norm_fn: A norm function applicable to
        a 2D numpy matrix representing the absolute difference between the
        oracle purity score matrix and the predicted purity score matrix. If not
        given then we will use the 2D Frobenius norm.
    :param np.ndarray oracle_matrix: If given, then this must be a 2D array with
        shape (n_concepts, n_concepts) such that the (i, j)-th entry represents
        the AUC of an oracle that predicts the value of concept j given the
        ground truth of concept i. If not given, then this matrix will be
        computed using the ground truth concept labels.
    :param np.ndarray purity_matrix: If given, then this must be a 2D array with
        shape (n_concepts, n_concepts) such that the (i, j)-th entry represents
        the AUC of predicting the value of concept j given the soft
        representation of concept i. If not given, then this matrix will be
        computed using the purity scores from the input soft representations.
    :param bool output_matrices: If True then this method will output a tuple
        (score, purity_matrix, oracle_matrix) containing the computed purity
        score, purity matrix, and oracle matrix given this function's
        arguments.
    :param Function[(np.ndarray), np.ndarray] alignment_function: an optional
        alignment function that takes as an input an (k, n_concepts) purity
        matrix, where k >= n_concepts and its (i, j) value is the AUC of
        predicting true concept j using soft representations i, and returns a
        (n_concepts, n_concepts) matrix where a subset of n_concepts soft
        concept representations has been aligned in a bijective fashion with
        the set of all ground truth concepts.


    :returns Or[Tuple[float, np.ndarray, np.ndarray], float]: If output_matrices
        is False (default behavior) then the output will be a non-negative float
        in [0, 1] representing the degree to which individual concepts
        representations encode unnecessary information for other concepts. Higher
        values mean more impurity and the concepts are considered to be pure if
        the returned value is 0. If output_matrices is True, then the output
        will be a tuple (score, purity_matrix, oracle_matrix) containing the
        computed purity score, purity matrix, and oracle matrix given this
        function's arguments. If alignment_function is given, then the purity
        matrix will be a tuple (purity_matrix, aligned_purity_matrix) containing
        the pre and post alignment purity matrices, respectively.
    """

    # Now the concept_label_cardinality vector from the given soft labels
    (n_samples, n_concepts) = c_true.shape
    if concept_label_cardinality is None:
        concept_label_cardinality = [
            len(set(c_true[:, i]))
            for i in range(n_concepts)
        ]
    # First compute the predictor soft-concept purity matrix
    if purity_matrix is not None:
        pred_matrix = purity_matrix
    else:
        pred_matrix = concept_purity_matrix(
            c_soft=c_soft,
            c_true=c_true,
            predictor_model_fn=predictor_model_fn,
            predictor_train_kwags=predictor_train_kwags,
            test_size=test_size,
            concept_label_cardinality=concept_label_cardinality,
            jointly_learnt=jointly_learnt,
        )

    # Compute the oracle's purity matrix
    if oracle_matrix is None:
        oracle_matrix = oracle_purity_matrix(
            concepts=c_true,
            concept_label_cardinality=concept_label_cardinality,
            predictor_model_fn=predictor_model_fn,
            predictor_train_kwags=predictor_train_kwags,
            test_size=test_size,
            jointly_learnt=jointly_learnt,
        )

    # Finally, compute the norm of the absolute difference between the two
    # matrices
    if alignment_function is not None:
        # Then lets make sure we align our prediction matrix correctly
        aligned_matrix = alignment_function(pred_matrix)
        if not include_diagonal:
            used_aligned_matrix = np.copy(aligned_matrix)
            np.fill_diagonal(used_aligned_matrix, 1)
            used_oracle_matrix = np.copy(oracle_matrix)
            np.fill_diagonal(used_oracle_matrix, 1)
        else:
            used_oracle_matrix = oracle_matrix
            used_aligned_matrix = aligned_matrix
        score = norm_fn(np.abs(used_oracle_matrix - used_aligned_matrix))
        if output_matrices:
            return score, (pred_matrix, aligned_matrix), oracle_matrix
        return score

    if not include_diagonal:
        used_pred_matrix = np.copy(pred_matrix)
        np.fill_diagonal(used_pred_matrix, 1)
        used_oracle_matrix = np.copy(oracle_matrix)
        np.fill_diagonal(used_oracle_matrix, 1)
    else:
        used_oracle_matrix = oracle_matrix
        used_pred_matrix = pred_matrix
    score = normalize_impurity(
        impurity=norm_fn(np.abs(used_oracle_matrix - used_pred_matrix)),
        n_concepts=n_concepts,
    )
    if output_matrices:
        return score, pred_matrix, oracle_matrix
    return score


def encoder_oracle_impurity_score(
    encoder_model,
    features,
    concepts,
    predictor_model_fn=None,
    predictor_train_kwags=None,
    test_size=0.2,
    norm_fn=lambda x: np.linalg.norm(x, ord='fro'),
    oracle_matrix=None,
    output_matrices=False,
    purity_matrix=None,
    alignment_function=None,
    include_diagonal=True,
):
    """
    Returns the OIS of the concept representations generated by
    `encoder_model` when given `features` with respect to their corresponding
    ground truth concepts `concepts`. This value is higher if concepts encode
    unnecessary information from other concepts in their soft representation and
    lower otherwise. If zero, then all soft concept labels are considered to be
    "pure".

    We compute this metric by calculating the norm of the absolute difference
    between the purity matrix derived from the soft concepts and the purity
    matrix derived from an oracle model. This oracle model is trained using
    the ground truth labels instead of the soft labels and may capture trivial
    relationships between different concept labels.

    :param skelearn-like Estimator encoder_model: An encoder estimator capable
        of extracting concepts from a set of features. This estimator may
        produce a vector of binary concept probabilities for each sample (i.e.,
        in the case of all concepts being binary) or a list of vectors
        representing probability distributions over the labels for each concept
        (i.e., in the case of one or more concepts being categorical).
    :param np.ndarray features: An array of testing samples with shape
        (n_samples, ...) used to compute the purity matrix.
    :param np.ndarray concepts: Ground truth concept values in one-to-one
        correspondence with samples in features. Shape must be
        (n_samples, n_concepts).
    :param Function[(int,), sklearn-like Estimator] predictor_model_fn: A
        function generator that takes as an argument the number of
        the output target concept and produces an sklearn-like Estimator
        which one can train for predicting a concept given another concept's
        soft concept values. If not given then we will use a 3-layer ReLU MLP
        as our predictor.
    :param Dict[Any, Any] predictor_train_kwags: optional arguments to pass
        the estimator when calling its `fit` method.
    :param float test_size: A value in [0, 1] indicating the fraction of the
        given data that will be used to evaluate the trained concept-based
        classifier. The rest of the data will be used for training said
        classifier.
    :param Function[(np.ndarray), float] norm_fn: A norm function applicable to
        a 2D numpy matrix representing the absolute difference between the
        oracle purity score matrix and the predicted purity score matrix. If not
        given then we will use the 2D Frobenius norm.
    :param np.ndarray oracle_matrix: If given, then this must be a 2D array with
        shape (n_concepts, n_concepts) such that the (i, j)-th entry represents
        the AUC of an oracle that predicts the value of concept j given the
        ground truth of concept i. If not given, then this matrix will be
        computed using the ground truth concept labels.
    :param np.ndarray purity_matrix: If given, then this must be a 2D array with
        shape (n_concepts, n_concepts) such that the (i, j)-th entry represents
        the AUC of predicting the value of concept j given the soft
        representation generated by the encoder for concept i. If not given,
        then this matrix will be computed using the purity scores from the input
        encoder's soft representations.
    :param bool output_matrices: If True then this method will output a tuple
        (score, purity_matrix, oracle_matrix) containing the computed purity
        score, purity matrix, and oracle matrix given this function's
        arguments.
    :param Function[(np.ndarray,), np.ndarray] alignment_function: an optional
        alignment function that takes as an input an (k, n_concepts) purity
        matrix, where k >= n_concepts and its (i, j) value is the AUC of
        predicting true concept j using soft representations i, and returns a
        (n_concepts, n_concepts) matrix where a subset of n_concepts soft
        concept representations has been aligned in a bijective fashion with
        the set of all ground truth concepts.

    :returns Or[Tuple[float, np.ndarray, np.ndarray], float]: If output_matrices
        is False (default behavior) then the output will be a non-negative float
        representing the degree to which individual concepts in the given
        bottleneck encode unnecessary information for other concepts. Higher
        values mean more impurity and the concepts are considered to be pure if
        the returned value is 0. If output_matrices is True, then the output
        will be a tuple (score, purity_matrix, oracle_matrix) containing the
        computed purity score, purity matrix, and oracle matrix given this
        function's arguments. If alignment_function is given, then the purity
        matrix will be a tuple (purity_matrix, aligned_purity_matrix) containing
        the pre and post alignment purity matrices, respectively.
    """
    # Simply use the concept purity metric defined above when given
    # soft concepts as computed by the encoder model
    return oracle_impurity_score(
        c_soft=encoder_model.predict(features),
        c_true=concepts,
        predictor_model_fn=predictor_model_fn,
        predictor_train_kwags=predictor_train_kwags,
        test_size=test_size,
        norm_fn=norm_fn,
        oracle_matrix=oracle_matrix,
        purity_matrix=purity_matrix,
        output_matrices=output_matrices,
        alignment_function=alignment_function,
        include_diagonal=include_diagonal,
    )

#################################################

# cem.metrics.cas

#################################################





warnings.simplefilter("ignore", UserWarning)

def concept_alignment_score(
    c_vec,
    c_test,
    y_test,
    step,
    force_alignment=False,
    alignment=None,
    progress_bar=True,
):
    """
    Computes the concept alignment score between learnt concepts and labels.

    :param c_vec: predicted concept representations (can be concept embeddings)
    :param c_test: concept ground truth labels
    :param y_test: task ground truth labels
    :param step: number of integration steps
    :return: concept alignment AUC, task alignment AUC
    """

    # First lets compute an alignment between concept
    # scores and ground truth concepts
    if force_alignment:
        if alignment is None:
            purity_mat = concept_purity_matrix(
                c_soft=c_vec,
                c_true=c_test,
            )
            alignment = find_max_alignment(purity_mat)
        # And use the new vector with its corresponding alignment
        if c_vec.shape[-1] < c_test.shape[-1]:
            # Then the alignment will need to be done backwards as
            # we will have to get rid of the dimensions in c_test
            # which have no aligment at all
            c_test = c_test[:, list(filter(lambda x: x is not None, alignment))]
        else:
            c_vec = c_vec[:, alignment]

    # compute the maximum value for the AUC
    n_clusters = np.linspace(
        2,
        c_vec.shape[0],
        step,
    ).astype(int)
    print("in cas c_vec shape is", c_vec.shape)
    print("n_clusters is", n_clusters)
    print("step is", step)
    max_auc = np.trapz(np.ones(len(n_clusters)))

    # for each concept:
    #   1. find clusters
    #   2. compare cluster assignments with ground truth concept/task labels
    concept_auc, task_auc = [], []
    if progress_bar:
        bar = tqdm(range(c_test.shape[1]))
    else:
        bar = range(c_test.shape[1])
    for concept_id in bar:
        concept_homogeneity, task_homogeneity = [], []
        for nc in n_clusters:
            kmedoids = KMedoids(n_clusters=nc, random_state=0)
            if c_vec.shape[1] != c_test.shape[1]:
                c_cluster_labels = kmedoids.fit_predict(
                    np.hstack([
                        c_vec[:, concept_id][:, np.newaxis],
                        c_vec[:, c_test.shape[1]:]
                    ])
                )
            elif c_vec.shape[1] == c_test.shape[1] and len(c_vec.shape) == 2:
                c_cluster_labels = kmedoids.fit_predict(
                    c_vec[:, concept_id].reshape(-1, 1)
                )
            else:
                c_cluster_labels = kmedoids.fit_predict(c_vec[:, concept_id, :])

            # compute alignment with ground truth labels
            concept_homogeneity.append(
                homogeneity_score(c_test[:, concept_id], c_cluster_labels)
            )
            task_homogeneity.append(
                homogeneity_score(y_test, c_cluster_labels)
            )

        # compute the area under the curve
        concept_auc.append(np.trapz(np.array(concept_homogeneity)) / max_auc)
        task_auc.append(np.trapz(np.array(task_homogeneity)) / max_auc)

    # return the average alignment across all concepts
    concept_auc = np.mean(concept_auc)
    task_auc = np.mean(task_auc)
    if force_alignment:
        return concept_auc, task_auc, alignment
    return concept_auc, task_auc

#######################################

# cem.metrics.niching

#######################################

"""
Niche Impurity Score (NIS) taken from
https://github.com/mateoespinosa/concept-quality.

All credit goes to Espinosa Zarlenga et al. AAAI 2023.
"""



def niche_completeness(c_pred, y_true, predictor_model, niches):
    '''
    Computes the niche completeness score for the downstream task
    :param c_pred: Concept data predictions, numpy array of shape
        (n_samples, n_concepts)
    :param y_true: Ground-truth task label data, numpy array of shape
        (n_samples, n_tasks)
    :param predictor_model: trained decoder model to use for predicting the task
        labels from the concept data
    :return: Accuracy of predictor_model, evaluated on niches obtained from the
        provided concept and label data
    '''
    n_tasks = y_true.shape[1]
    # compute niche completeness for each task
    niche_completeness_list, y_pred_list = [], []
    for task in range(n_tasks):
        # find niche
        niche = np.zeros_like(c_pred)
        niche[:, niches[:, task] > 0] = c_pred[:, niches[:, task] > 0]

        # compute task predictions
        y_pred_niche = predictor_model.predict_proba(niche)
        if predictor_model.__class__.__name__ == 'Sequential':
            # get class labels from logits
            y_pred_niche = y_pred_niche > 0
        elif len(y_pred_niche.shape) == 1:
            y_pred_niche = y_pred_niche[:, np.newaxis]

        y_pred_list.append(y_pred_niche[:, task])

    y_preds = np.vstack(y_pred_list).T
    y_preds = softmax(y_preds, axis=1)
    auc = roc_auc_score(y_true, y_preds, multi_class='ovo')

    result = {
        'auc_completeness': auc,
        'y_preds': y_preds,
    }
    return result


def niche_completeness_ratio(c_pred, y_true, predictor_model, niches):
    '''
    Computes the niche completeness ratio for the downstream task
    :param c_pred: Concept data predictions, numpy array of shape
        (n_samples, n_concepts)
    :param y_true: Ground-truth task label data, numpy array of shape
        (n_samples, n_tasks)
    :param predictor_model: sklearn model to use for predicting the task labels
        from the concept data
    :return: Accuracy ratio between the accuracy of predictor_model evaluated
        on niches and the accuracy of predictor_model evaluated on all concepts
    '''
    n_tasks = y_true.shape[1]

    y_pred_test = predictor_model.predict_proba(c_pred)
    if predictor_model.__class__.__name__ == 'Sequential':
        # get class labels from logits
        y_pred_test = y_pred_test > 0
    elif len(y_pred_test.shape) == 1:
        y_pred_test = y_pred_test[:, np.newaxis]

    # compute niche completeness for each task
    niche_completeness_list = []
    for task in range(n_tasks):
        # find niche
        niche = np.zeros_like(c_pred)
        niche[:, niches[:, task] > 0] = c_pred[:, niches[:, task] > 0]

        # compute task predictions
        y_pred_niche = predictor_model.predict_proba(niche)
        if predictor_model.__class__.__name__ == 'Sequential':
            # get class labels from logits
            y_pred_niche = y_pred_niche > 0
        elif len(y_pred_niche.shape) == 1:
            y_pred_niche = y_pred_niche[:, np.newaxis]

        # compute accuracies
        accuracy_base = accuracy_score(y_true[:, task], y_pred_test[:, task])
        accuracy_niche = accuracy_score(y_true[:, task], y_pred_niche[:, task])

        # compute the accuracy ratio of the niche w.r.t. the baseline
        # (full concept bottleneck) the higher the better (high predictive power
        # of the niche)
        niche_completeness = accuracy_niche / accuracy_base
        niche_completeness_list.append(niche_completeness)

    result = {
        'niche_completeness_ratio_mean': np.mean(niche_completeness_list),
        'niche_completeness_ratio': niche_completeness_list,
    }
    return result


def niche_impurity(c_pred, y_true, predictor_model, niches):
    '''
    Computes the niche impurity score for the downstream task
    :param c_pred: Concept data predictions, numpy array of shape
        (n_samples, n_concepts)
    :param y_true: Ground-truth task label data, numpy array of shape
        (n_samples, n_tasks)
    :param predictor_model: sklearn model to use for predicting the task labels
        from the concept data
    :return: Accuracy ratio between the accuracy of predictor_model evaluated on
        concepts outside niches and the accuracy of predictor_model evaluated on
        concepts inside niches
    '''
    n_tasks = y_true.shape[1]

    if len(c_pred.shape) == 2:
        n_samples, n_concepts = c_pred.shape
        assert n_concepts == n_tasks, 'Number of concepts and tasks must be equal'

        # compute niche completeness for each task
        y_pred_list = []

        nis = 0.0
        count = 0
        for i in range(n_concepts):
            if len(np.unique(y_true[:, i])) == 1:
                continue
            count += 1
            # find niche
            niche = np.zeros_like(c_pred)
            niche[:, niches[:, i] > 0] = c_pred[:, niches[:, i] > 0]

            # find concepts outside the niche
            niche_out = np.zeros_like(c_pred)
            niche_out[:, niches[:, i] <= 0] = c_pred[:, niches[:, i] <= 0]

            # compute task predictions
            y_pred_niche = predictor_model.predict_proba(niche)
            y_pred_niche_out = predictor_model.predict_proba(niche_out)
            if predictor_model.__class__.__name__ == 'Sequential':
                # get class labels from logits
                y_pred_niche_out = y_pred_niche_out > 0
            elif len(y_pred_niche.shape) == 1:
                y_pred_niche_out = y_pred_niche_out[:, np.newaxis]

            nis += roc_auc_score(y_true[:, i], y_pred_niche_out[:, i])
        if count:
            nis = nis / count
    else:
        n_samples, h_concepts, n_concepts = c_pred.shape
        assert n_concepts == n_tasks, 'Number of concepts and tasks must be equal'
        c_soft_test2 = c_pred.reshape(-1, h_concepts*n_concepts)
        nis = 0.0
        count = 0
        for i in range(n_concepts):
            if len(np.unique(y_true[:, i])) == 1:
                continue
            count += 1
            c_soft_test3 = c_soft_test2.copy()
            mask = np.repeat(niches[:, i], h_concepts)
            c_soft_test_masked = c_soft_test3
            c_soft_test_masked[:, mask] = 0
            c_pred_niche = predictor_model.predict_proba(c_soft_test_masked)[:, i]

            c_soft_test3 = c_soft_test2.copy()
            c_soft_test_masked = c_soft_test3
            c_soft_test_masked[:, ~mask] = 0
            c_pred_niche = predictor_model.predict_proba(c_soft_test_masked)[:, i]
            nis += roc_auc_score(
                y_true[:, i],
                c_pred_niche,
            )
        if count:
            nis = nis / count
    return nis


def niche_finding(c, y, mode='mi', threshold=0.5):
    n_concepts = c.shape[-1]
    n_targets = y.shape[-1]
    if len(c.shape) == 3:
        # Multi-dimensional concept representation case!
        n_samples, h_concepts, n_concepts = c.shape
        niching_matrix = np.zeros((n_concepts, n_targets))
        for j in range(n_targets):
            for i in range(n_concepts):
                corrm = np.corrcoef(np.hstack([c[:, :, i], y[:, j].reshape(-1, 1)]).T)
                nm = corrm[:h_concepts, h_concepts:]
                niching_matrix[i, j] = nm.max()
        niches = niching_matrix > threshold
    else:
        if mode == 'corr':
            corrm = np.corrcoef(np.hstack([c, y]).T)
            niching_matrix = corrm[:n_concepts, n_concepts:]
            niches = np.abs(niching_matrix) > threshold
        elif mode == 'mi':
            nm = []
            for yj in y.T:
                mi = mutual_info_classif(c, yj)
                nm.append(mi)
            nm = np.vstack(nm).T
            niching_matrix = nm / np.max(nm)
            niches = niching_matrix > threshold
        else:
            return None, None

    return niches, niching_matrix


def niching_high_dim(
    c_soft_train,
    c_true_train,
    c_soft_test,
    c_true_test,
    classifier,
    threshold=0.5,
):
    n_samples, h_concepts, n_concepts = c_soft_train.shape
    niching_matrix = np.zeros((n_concepts, n_concepts))
    for j in range(n_concepts):
        for i in range(n_concepts):
            corrm = np.corrcoef(
                np.hstack([c_soft_train[:, :, i], c_true_train[:, j].reshape(-1, 1)]).T
            )
            nm = corrm[:h_concepts, h_concepts:]
            niching_matrix[i, j] = nm.max()

    c_soft_train2 = c_soft_train.reshape(-1, h_concepts*n_concepts)
    c_soft_test2 = c_soft_test.reshape(-1, h_concepts*n_concepts)
    classifier.fit(c_soft_train2, c_true_train)

    c_preds_impurity = []
    niches = niching_matrix > threshold
    for i in range(n_concepts):
        c_soft_test3 = c_soft_test2.copy()
        mask = np.repeat(niches[:, i], h_concepts)
        c_soft_test_masked = c_soft_test3
        c_soft_test_masked[:, mask] = 0
        c_pred_niche = classifier.predict_proba(c_soft_test_masked)[:, i]

        c_soft_test3 = c_soft_test2.copy()
        c_soft_test_masked = c_soft_test3
        c_soft_test_masked[:, ~mask] = 0
        c_pred_niche = classifier.predict_proba(c_soft_test_masked)[:, i]
        c_preds_impurity.append(c_pred_niche)

    c_preds_impurity = np.stack(c_preds_impurity).T
    c_preds_impurity = softmax(c_preds_impurity, axis=1)
    return roc_auc_score(
        c_true_test.argmax(axis=1),
        c_preds_impurity,
        multi_class='ovo',
    )


def niche_impurity_score(
    c_soft,
    c_true,
    c_soft_train=None,
    c_true_train=None,
    predictor_model_fn=None,
    predictor_train_kwags=None,
    delta_beta=0.05,
    test_size=0.2,
):
    """
    Returns the niche impurity score (NIS) of the given soft concept
    representations `c_soft` with respect to their corresponding ground truth
    concepts `c_true`. This value is higher if concepts encode unnecessary
    information from other concepts distributed across SUBSETS of soft concept
    representations, and lower otherwise.

    :param Or[np.ndarray, List[np.ndarray]] c_soft: Predicted set of "soft"
        concept representations by a concept encoder model applied to the
        testing data. This argument must be an np.ndarray with shape
        (n_samples, ..., n_concepts) where the concept representation may be
        of any rank as long as the last dimension is the dimension used to
        separate distinct concept representations. If concepts have distinct
        array shapes for their representations, then this argument is expected
        to be a list of `n_concepts` np.ndarrays where the i-th element in the
        list is an array with shape (n_samples, ...) containing the tensor
        representation of the i-th concept.
        Note that in either case we only require that the first dimension.
    :param np.ndarray c_true: Ground truth concept values in one-to-one
        correspondence with concepts in c_soft. Shape must be
        (n_samples, n_concepts).
    :param Function[(int,), sklearn-like Estimator] predictor_model_fn: A
        function generator that takes as an argument the number of
        the output target concept and produces an sklearn-like Estimator
        which one can train for predicting a concept given another concept's
        soft concept values. If not given then we will use a 3-layer ReLU MLP
        as our predictor.
    :param Dict[Any, Any] predictor_train_kwags: optional arguments to pass
        the estimator being when calling its `fit` method.
    :param float test_size: A value in [0, 1] indicating the fraction of the
        given data that will be used to evaluate the trained concept-based
        classifier. The rest of the data will be used for training said
        classifier.

    :returns float: A non-negative float in [0, 1] representing the degree to
        which individual concepts in the given representations encode
        unnecessary information regarding other concepts distributed across
        them.
    """
    (n_samples, n_concepts) = c_true.shape
    # finding niches for several values of beta
    niche_impurities = []

    if predictor_model_fn is None:
        predictor_model_fn = lambda n_concepts: MLPClassifier(
            (20, 20),
            random_state=1,
            max_iter=1000,
            batch_size=min(512, n_samples)
        )
    if predictor_train_kwags is None:
        predictor_train_kwags = {}
    if len(c_soft.shape) == 2 and c_soft.shape[1] == 1:
        # Then get rid of degenerate dimension for simplicity
        c_soft = np.reshape(c_soft, (-1, n_concepts))
    if c_soft_train is not None and (
        len(c_soft_train.shape) == 2 and c_soft_train.shape[1] == 1
    ):
        # Then get rid of degenerate dimension for simplicity
        c_soft_train = np.reshape(c_soft_train, (-1, n_concepts))

    # And estimate the area under the curve using the trapezoid method
    auc = 0
    prev_value = None
    classifier = predictor_model_fn(n_concepts=n_concepts)
    if (c_soft_train is None) and (c_true_train is None):
        c_soft_train, c_soft_test, c_true_train, c_true_test = train_test_split(
            c_soft,
            c_true,
            test_size=test_size,
        )
    else:
        c_true_test = c_true
        c_soft_test = c_soft

    if len(c_soft_train.shape) == 3:
        # Then we are working in the multi-dimensional case!
        # So we need to flattent the dimensions
        classifier.fit(
            c_soft_train.reshape(c_soft_train.shape[0], -1),
            c_true_train,
            **predictor_train_kwags,
        )
    else:
        classifier.fit(c_soft_train, c_true_train, **predictor_train_kwags)

    for beta in tqdm(np.arange(0.0, 1.0, delta_beta)):
        niches, _ = niche_finding(
            c_soft_train,
            c_true_train,
            mode='corr',
            threshold=beta,
        )
        # compute impurity scores
        nis_score = niche_impurity(
            c_soft_test,
            c_true_test,
            classifier,
            niches,
        )
        niche_impurities.append(nis_score)
        # And update the area under the curve
        if prev_value is not None:
            auc += (prev_value + nis_score) * (delta_beta / 2)
        prev_value = nis_score

    return auc
