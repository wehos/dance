"""Reimplementation of the JAE model, which is adapted from scDEC.

Extended from https://github.com/kimmo1019/JAE

Reference
---------
Liu Q, Chen S, Jiang R, et al. Simultaneous deep generative modelling and clustering of single-cell genomic data[J].
Nature machine intelligence, 2021, 3(6): 536-544.

"""

import os

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader

from dance.utils import SimpleIndexDataset
from dance.utils.metrics import *


def random_classification_loss(y_pred, nb_batches):
    device = nb_batches.device
    y_true = torch.ones(y_pred.shape).float().to(device) / nb_batches.shape[0]
    return (-(torch.softmax(y_pred, -1) + 1e-7).log() * y_true).sum(-1).mean()


class JAEWrapper:
    """JAE class.

    Parameters
    ----------
    args : argparse.Namespace
        A Namespace object that contains arguments of JAE. For details of parameters in parser args, please refer to
        link (parser help document).
    dataset : dance.datasets.multimodality.JointEmbeddingNIPSDataset
        Joint embedding dataset.

    """

    def __init__(self, args, dataset):
        self.model = JAE(dataset.nb_cell_types, dataset.nb_batches, dataset.nb_phases,
                         dataset.preprocessed_data['X_train'].shape[1]).to(args.device)
        self.args = args

    def fit(self, inputs, labels):
        """fit function for training.

        Parameters
        ----------
        inputs : torch.Tensor
            Modality features.
        labels : list[torch.Tensor]
            Multiple auxiliary labels for supervision.

        Returns
        -------
        None.

        """
        X = torch.from_numpy(inputs).float().to(self.args.device)
        Y = [
            torch.from_numpy(labels[0]).long().to(self.args.device),
            torch.from_numpy(labels[1]).long().to(self.args.device),
            torch.from_numpy(labels[3]).float().to(self.args.device)
        ]

        idx = np.random.permutation(X.shape[0])
        train_idx = idx[:int(idx.shape[0] * 0.9)]
        val_idx = idx[int(idx.shape[0] * 0.9):]

        train_dataset = SimpleIndexDataset(train_idx)
        train_loader = DataLoader(
            dataset=train_dataset,
            batch_size=self.args.batch_size,
            shuffle=True,
            num_workers=1,
        )

        ce = nn.CrossEntropyLoss()
        mse = nn.MSELoss()

        optimizer = torch.optim.Adam([{'params': self.model.parameters()}], lr=1e-4)
        vals = []

        for epoch in range(60):
            self.model.train()
            total_loss = [0] * 5
            print('epoch', epoch)

            for iter, batch_idx in enumerate(train_loader):

                batch_x = X[batch_idx]
                batch_y = [batch_x, Y[0][batch_idx], Y[1][batch_idx], Y[2][batch_idx]]

                output = self.model(batch_x)

                # loss1 = mse(output[0], batch_y[0]) # option 1: recover features after propagation
                loss1 = mse(output[0], batch_x)  # option 2: recover Isi features
                loss2 = ce(output[1], batch_y[1])
                loss3 = random_classification_loss(output[2], batch_y[2])
                loss4 = mse(output[3], batch_y[3])

                loss = loss1 * 0.7 + loss2 * 0.2 + loss3 * 0.05 + loss4 * 0.05

                total_loss[0] += loss1.item()
                total_loss[1] += loss2.item()
                total_loss[2] += loss3.item()
                total_loss[3] += loss4.item()
                total_loss[4] += loss.item()

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

            for i in range(4):
                print(f'loss{i + 1}', total_loss[i] / len(train_loader), end=', ')
            print()

            loss1, loss2, loss3, loss4 = self.score(X, val_idx, Y)
            weighted_loss = loss1 * 0.7 + loss2 * 0.2 + loss3 * 0.05 + loss4 * 0.05
            print('val-loss1', loss1, 'val-loss2', loss2, 'val-loss3', loss3, 'val-loss4', loss4)
            print('val score', weighted_loss)
            vals.append(weighted_loss)
            if min(vals) == vals[-1]:
                if not os.path.exists('models'):
                    os.mkdir('models')
                torch.save(self.model.state_dict(), f'models/model_joint_embedding_{self.args.rnd_seed}.pth')

            if min(vals) != min(vals[-10:]):
                break

    def to(self, device):
        """Performs device conversion.

        Parameters
        ----------
        device : str
            Target device.

        Returns
        -------
        self : JAEWrapper
            Converted model.

        """
        self.args.device = device
        self.model = self.model.to(device)
        return self

    def load(self, path, map_location=None):
        """Load model parameters from checkpoint file.

        Parameters
        ----------
        path : str
            Path to the checkpoint file.
        map_location : str optional
            Mapped device. This parameter will be passed to torch.load function if not none.

        Returns
        -------
        None.

        """
        if map_location is not None:
            self.model.load_state_dict(torch.load(path, map_location=map_location))
        else:
            self.model.load_state_dict(torch.load(path))

    def predict(self, inputs, idx):
        """Predict function to get latent representation of data.

        Parameters
        ----------
        inputs : torch.Tensor
            Multimodality features.
        idx : Iterable(int)
            Index of cells to predict.

        Returns
        -------
        prediction : torch.Tensor
            Joint embedding of input data.

        """
        self.model.eval()
        with torch.no_grad():
            prediction = self.model.encoder(inputs[idx])
        return prediction

    def score(self, inputs, idx, labels, metric='loss'):
        """Score function to get score of prediction.

        Parameters
        ----------
        inputs : torch.Tensor
            Multimodality features.
        idx : Iterable[int]
            Index of testing cells for scoring.
        labels : list[torch.Tensor]
            Multiple labels for evaluation.
        metric : str optional
            The type of evaluation metric, by default to be 'loss'.

        Returns
        -------
        loss1 : float
            Reconstruction loss.
        loss2 : float
            Cell type classfication loss.
        loss3 : float
            Batch regularization loss.
        loss4 : float
            Cell cycle score loss.

        """
        self.model.eval()

        with torch.no_grad():

            if metric == 'loss':
                ce = nn.CrossEntropyLoss()
                mse = nn.MSELoss()
                X = inputs[idx]
                output = self.model(X)
                loss1 = mse(output[0], X).item()
                loss2 = ce(output[1], labels[0][idx]).item()
                loss3 = random_classification_loss(output[2], labels[1][idx]).item()
                loss4 = mse(output[3], labels[2][idx]).item()

                return loss1, loss2, loss3, loss4
            else:
                emb = self.predict(inputs, idx).cpu().numpy()

                kmeans = KMeans(n_clusters=10, n_init=5, random_state=200)

                # adata.obs['batch'] = adata_sol.obs['batch'][adata.obs_names]
                # adata.obs['cell_type'] = adata_sol.obs['cell_type'][adata.obs_names]
                true_labels = labels
                pred_labels = kmeans.fit_predict(emb)
                NMI_score = round(normalized_mutual_info_score(true_labels, pred_labels, average_method='max'), 3)
                ARI_score = round(adjusted_rand_score(true_labels, pred_labels), 3)

                # print('ARI: ' + str(ARI_score) + ' NMI: ' + str(NMI_score))
                return NMI_score, ARI_score


class JAE(nn.Module):

    def __init__(self, nb_cell_types, nb_batches, nb_phases, input_dimension):
        super().__init__()
        self.nb_cell_types = nb_cell_types
        self.nb_batches = nb_batches
        self.nb_phases = nb_phases

        self.linear1 = nn.Linear(input_dimension, 150)
        self.linear2 = nn.Linear(150, 120)
        self.linear3 = nn.Linear(120, 100)
        self.linear4 = nn.Linear(100, 61)

        self.bn1 = nn.BatchNorm1d(150)
        self.bn2 = nn.BatchNorm1d(120)
        self.bn3 = nn.BatchNorm1d(100)

        self.act1 = nn.GELU()
        self.act2 = nn.GELU()
        self.act3 = nn.GELU()

        self.decoder = nn.Sequential(
            nn.Linear(61, 150),
            nn.ReLU(),
            nn.Linear(150, input_dimension),
            nn.ReLU(),
        )

    def encoder(self, x):
        x = self.linear1(x)
        x = self.act1(x)
        x = self.bn1(x)
        x = F.dropout(x, p=0.2, training=self.training)

        x = self.linear2(x)
        x = self.act2(x)
        x = self.bn2(x)
        x = F.dropout(x, p=0.2, training=self.training)

        x = self.linear3(x)
        x = self.act3(x)
        x = self.bn3(x)
        x = F.dropout(x, p=0.2, training=self.training)

        x = self.linear4(x)
        return x

    def forward(self, x):
        x = self.encoder(x)
        x0 = x
        x = self.decoder(x)

        return x, x0[:, :self.nb_cell_types], x0[:, self.nb_cell_types:self.nb_cell_types + self.nb_batches], \
               x0[:, self.nb_cell_types + self.nb_batches:self.nb_cell_types + self.nb_batches + self.nb_phases]
