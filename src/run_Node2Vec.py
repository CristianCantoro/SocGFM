import os
import mlflow
import shutil
import numpy as np
import networkx as nx

from data_loader import create_data_loader
from model_eval import TestLogMetrics, eval_pred
from my_utils import set_seed, setup_env, move_data_to_device
from node2vec import Node2Vec
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier

DEFAULT_HYPERPARAMETERS = {'train_perc': 0.7,
                           'val_perc': 0.15,
                           'test_perc': 0.15,
                           'overwrite_data': False}
DEFAULT_TRAIN_HYPERPARAMETERS = {}
DEFAULT_MODEL_HYPERPARAMETERS = {'latent_dim': 32}


def create_model(model_hyperparameters):
    if model_hyperparameters["model_name"] == 'LR':
        return LogisticRegression()
    elif model_hyperparameters["model_name"] == 'RF':
        return RandomForestClassifier()
    model_name = model_hyperparameters["model_name"]
    raise Exception(f'{model_name} not allowed.')


# noinspection PyShadowingNames
def run_experiment(dataset_name='cuba',
                   is_few_shot=False,
                   num_splits=10,
                   device_id="",
                   seed=0,
                   hyper_parameters=DEFAULT_HYPERPARAMETERS,
                   train_hyperparameters=DEFAULT_TRAIN_HYPERPARAMETERS,
                   model_hyper_parameters=DEFAULT_MODEL_HYPERPARAMETERS
                   ):
    # Start experiment
    # save parameters
    mlflow.log_param('dataset_name', dataset_name)

    # set seed for reproducibility
    set_seed(seed)
    # set device
    os.environ['CUDA_VISIBLE_DEVICES'] = device_id
    device, base_dir, interim_data_dir, data_dir = setup_env(device_id, dataset_name, seed, num_splits,
                                                             is_few_shot,
                                                             hyper_parameters)
    print(data_dir)
    # Create data loader for signed datasets
    datasets = create_data_loader(dataset_name, base_dir, data_dir, hyper_parameters)

    # Transfer data to device
    datasets = move_data_to_device(datasets, device)

    # Precompute probabilities and generate walks - **ON WINDOWS ONLY WORKS WITH workers=1**
    node2vec = Node2Vec(datasets['graph'], dimensions=model_hyper_parameters['latent_dim'],
                        walk_length=5, num_walks=10, workers=8, seed=seed)
    # Embed nodes
    model = node2vec.fit(window=8, min_count=1, batch_words=4, seed=seed)
    node_embeddings_node2vec = np.full(
        shape=(datasets['graph'].number_of_nodes(), model_hyper_parameters['latent_dim']),
        fill_value=None)
    for node_id in datasets['graph'].nodes():
        node_embeddings_node2vec[int(node_id)] = model.wv[node_id]

    # Create loggers
    val_logger = TestLogMetrics(num_splits, ['accuracy', 'precision', 'f1_macro', 'f1_micro'])
    test_logger = TestLogMetrics(num_splits, ['accuracy', 'precision', 'f1_macro', 'f1_micro'])

    for run_id in range(num_splits):
        print(f'Split {run_id + 1}/{num_splits}')

        # Create the model
        model = create_model(model_hyper_parameters)
        model.fit(node_embeddings_node2vec[datasets['splits'][run_id]['train']],
                  datasets['labels'][datasets['splits'][run_id]['train']])
        # Evaluate perfomance on val set
        pred = model.predict(node_embeddings_node2vec)
        # Compute test statistics
        val_metrics = eval_pred(datasets['labels'], pred, datasets['splits'][run_id]['val'])
        for metric_name in test_metrics:
            val_logger.update(metric_name, run_id, val_metrics[metric_name])
        # Evaluate perfomance on test set
        test_pred = model.predict(node_embeddings_node2vec)
        # Compute test statistics
        test_metrics = eval_pred(datasets['labels'], pred, datasets['splits'][run_id]['test'])
        for metric_name in test_metrics:
            test_logger.update(metric_name, run_id, test_metrics[metric_name])

    print('Val set: ')
    for metric_name in val_logger.test_metrics_dict:
        avg_val, std_val = val_logger.get_metric_stats(metric_name)
        mlflow.log_metric(metric_name + '_avg', avg_val)
        mlflow.log_metric(metric_name + '_std', std_val)
        np.save(file=interim_data_dir / f'val_{metric_name}', arr=np.array(val_logger.test_metrics_dict[metric_name]))
        mlflow.log_artifact(interim_data_dir / f'val_{metric_name}.npy')
        print(f'[VAL] {metric_name}: {avg_val}+-{std_val}')

    print('Test set: ')
    for metric_name in test_logger.test_metrics_dict:
        avg_val, std_val = test_logger.get_metric_stats(metric_name)
        mlflow.log_metric(metric_name + '_avg', avg_val)
        mlflow.log_metric(metric_name + '_std', std_val)
        np.save(file=interim_data_dir / f'{metric_name}', arr=np.array(test_logger.test_metrics_dict[metric_name]))
        mlflow.log_artifact(interim_data_dir / f'{metric_name}.npy')
        print(f'[TEST] {metric_name}: {avg_val}+-{std_val}')


if __name__ == '__main__':
    # Run input parameters
    dataset_name = 'cuba'
    train_perc = 0.70
    val_perc = 0.15
    test_perc = 0.15
    overwrite_data = False
    is_few_shot = False
    seed = [0, ]
    num_splits = [10, ]
    # General hyperparameters
    hyper_parameters = {'train_perc': train_perc, 'val_perc': val_perc, 'test_perc': test_perc,
                        'overwrite_data': overwrite_data, 'traces_list': ['coRT']}
    # optimization hyperparameters
    train_hyper_parameters = {}
    # model hyperparameters
    latent_dim = 64
    model_hyper_parameters = {'latent_dim': latent_dim, 'model_name': 'RF'}
    for seed_val in seed:
        mlflow.set_experiment(f'{dataset_name}-Node2Vec-{seed_val}')
        for num_splits_val in num_splits:
            with mlflow.start_run():
                exp_dir = run_experiment(dataset_name=dataset_name,
                                         is_few_shot=is_few_shot,
                                         num_splits=num_splits_val,
                                         seed=seed_val,
                                         hyper_parameters=hyper_parameters,
                                         train_hyperparameters=train_hyper_parameters,
                                         model_hyper_parameters=model_hyper_parameters
                                         )
                try:
                    shutil.rmtree(exp_dir, ignore_errors=True)
                except OSError as e:
                    print("Error: %s - %s." % (e.filename, e.strerror))
