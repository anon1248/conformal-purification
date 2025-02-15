""" Module for ConformalProden. """

import numpy as np
import torch
from torch.utils.data import DataLoader, TensorDataset

from partial_label_learning.pll_classifier_base import PllBaseClassifier
from partial_label_learning.result import SplitResult


class ConformalProden(PllBaseClassifier):
    """
    ConformalProden.
    """

    def fit(
        self, inputs: np.ndarray, partial_targets: np.ndarray,
    ) -> SplitResult:
        """ Fits the model to the given inputs.

        Args:
            inputs (np.ndarray): The inputs.
            partial_targets (np.ndarray): The partial targets.

        Returns:
            SplitResult: The disambiguated targets.
        """

        # Separate validation set
        num_val = max(100, int(inputs.shape[0] * 0.2))
        all_indices = np.arange(inputs.shape[0])
        sup_mask = partial_targets.sum(axis=1) == 1
        sup_ind = np.arange(inputs.shape[0])[sup_mask]
        self.rng.shuffle(all_indices)
        self.rng.shuffle(sup_ind)
        if inputs.shape[0] / partial_targets.shape[1] < 1000:
            train_idx = all_indices.copy()  # Use all data if too few samples
        else:
            train_idx = all_indices[:-num_val]
        if sup_ind.shape[0] >= num_val:
            val_idx = sup_ind.copy()  # Use supervised samples if enough
        else:
            val_idx = all_indices[-num_val:]
        num_val = val_idx.shape[0]

        # Data preparation
        x_train = torch.tensor(inputs[train_idx], dtype=torch.float32)
        y_train = torch.tensor(partial_targets[train_idx], dtype=torch.float32)
        train_indices = torch.arange(x_train.shape[0], dtype=torch.int32)
        loss_weights = torch.tensor(
            partial_targets[train_idx], dtype=torch.float32)
        loss_weights /= loss_weights.sum(dim=1, keepdim=True)
        data_loader = DataLoader(
            TensorDataset(train_indices, x_train, y_train, loss_weights),
            batch_size=self.batch_size, shuffle=True,
        )

        # Validation set
        val_batch_size = self.batch_size
        x_val = torch.tensor(inputs[val_idx], dtype=torch.float32)
        y_val = torch.tensor(partial_targets[val_idx], dtype=torch.float32)
        val_dataloader = DataLoader(
            TensorDataset(x_val, y_val),
            batch_size=val_batch_size, shuffle=False,
        )

        # Optimizer
        self.model.train()
        optimizer = torch.optim.Adam(
            self.model.parameters(), lr=self.max_lr,
            weight_decay=self.weight_decay,
        )
        scheduler = torch.optim.lr_scheduler.OneCycleLR(
            optimizer, max_lr=self.max_lr, epochs=self.num_epoch,
            steps_per_epoch=len(data_loader),
        )

        # Training loop
        non_conformities_val: torch.Tensor = torch.zeros(
            x_val.shape[0], dtype=torch.float32)
        for epoch in self.loop_wrapper(range(self.num_epoch)):
            # Compute non-conformities on validation set
            self.model.eval()
            with torch.no_grad():
                for batch_idx, (x_i, y_i) in enumerate(val_dataloader):
                    x_i = x_i.to(self.device)
                    pred = self.model(x_i)[0].cpu()

                    # Take the minimum non-conformity
                    # among all candidates as reference
                    non_conformity = 1 - pred
                    non_conformity = torch.where(
                        y_i == 1, non_conformity, torch.inf)
                    non_conformity = torch.min(non_conformity, dim=1).values
                    non_conformities_val[
                        (batch_idx * val_batch_size):((batch_idx + 1) * val_batch_size)
                    ] = non_conformity

                # Sort non-conformities for ranking
                non_conformities_val[:] = non_conformities_val.sort().values

            # Train and purify train set
            self.model.train()
            for idx, inputs_i, partial_targets_i, w_ij in data_loader:
                # Forward-backward pass
                inputs_i = inputs_i.to(self.device)
                w_ij = w_ij.to(self.device)
                probs = self.model(inputs_i)[0]
                loss = torch.mean(torch.sum(
                    w_ij * -torch.log(probs + 1e-10), dim=1,
                ))
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                scheduler.step()

                # Update weights
                with torch.no_grad():
                    probs = probs.cpu()

                    if epoch >= 10:
                        # Use mean weights on non-candidates as a proxy
                        # for the likelihood of misprediction
                        alpha = 0.05
                        beta = torch.mean(
                            torch.sum(probs * (1 - partial_targets_i), dim=1))
                        eps = 0.5 ** (epoch - 9)

                        # Get non-conformity per class
                        new_nonconf = 1 - probs * (1 - eps)
                        ranks = num_val - torch.searchsorted(
                            non_conformities_val, new_nonconf)
                        p_vals = (ranks + 1) / (num_val + 1)
                        conformal_pred = torch.where(
                            p_vals > alpha + beta, 1.0, 0.0)
                    else:
                        conformal_pred = 1

                    common_filter = conformal_pred * partial_targets_i
                    w_filter = torch.where(
                        common_filter.sum(dim=1, keepdim=True) >= 1.0,
                        common_filter, partial_targets_i,
                    )
                    updated_w = w_filter * probs
                    updated_w /= torch.sum(updated_w, dim=1, keepdim=True)
                    loss_weights[idx] = updated_w

        # Predict all
        self.model.eval()
        with torch.no_grad():
            x_train_all = torch.tensor(inputs, dtype=torch.float32)
            y_train_all = torch.tensor(partial_targets, dtype=torch.float32)
            inference_dataloader = DataLoader(
                TensorDataset(x_train_all, y_train_all),
                batch_size=self.batch_size, shuffle=False,
            )
            all_results = []
            for x_i, s_i in inference_dataloader:
                x_i = x_i.to(self.device)
                s_i = s_i.to(self.device)
                probs = s_i * self.model(x_i)[0]
                all_results.append(probs.cpu().numpy())

        # Return results
        return SplitResult.from_scores(self.rng, np.vstack(all_results))

    def predict(self, inputs: np.ndarray) -> SplitResult:
        """ Predict the labels.

        Args:
            inputs (np.ndarray): The inputs.

        Returns:
            SplitResult: The predictions.
        """

        inference_loader = DataLoader(
            TensorDataset(torch.tensor(
                inputs, dtype=torch.float32)),
            batch_size=self.batch_size, shuffle=False,
        )

        # Switch to eval mode
        self.model.eval()
        all_results = []
        with torch.no_grad():
            for x_batch in inference_loader:
                x_batch = x_batch[0].to(self.device)
                all_results.append(
                    self.model(x_batch)[0].cpu().numpy())
            train_probs = np.vstack(all_results)
        return SplitResult.from_scores(self.rng, train_probs)
