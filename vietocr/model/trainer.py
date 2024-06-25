import os
import time

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch_pruning as tp
import torchvision
import yaml
from einops import rearrange
from PIL import Image
from torch import nn
from torch.optim import SGD, Adam, AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR, CyclicLR, OneCycleLR
from torch.utils.data import DataLoader

from vietocr.loader.aug import ImgAugTransform, ImgAugTransformV2
from vietocr.loader.dataloader import ClusterRandomSampler, Collator, OCRDataset
from vietocr.loader.dataloader_v1 import DataGen
from vietocr.optim.labelsmoothingloss import LabelSmoothingLoss
from vietocr.optim.optim import ScheduledOptim
from vietocr.tool.logger import Logger
from vietocr.tool.translate import batch_translate_beam_search, build_model, translate
from vietocr.tool.utils import compute_accuracy, download_weights


class Trainer:
    def __init__(self, config, pretrained=True, augmentor=ImgAugTransformV2()):

        self.config = config
        self.model, self.vocab = build_model(config)

        self.device = config["device"]
        self.num_epochs = config["trainer"]["num_epochs"]
        self.beamsearch = config["predictor"]["beamsearch"]

        self.data_root = config["dataset"]["data_root"]
        self.train_annotation = config["dataset"]["train_annotation"]
        self.valid_annotation = config["dataset"]["valid_annotation"]
        self.dataset_name = config["dataset"]["name"]

        self.batch_size = config["trainer"]["batch_size"]
        self.print_every = config["trainer"]["print_every"]
        self.valid_every = config["trainer"]["valid_every"]

        self.image_aug = config["aug"]["image_aug"]
        self.masked_language_model = config["aug"]["masked_language_model"]

        self.checkpoint = config["trainer"]["checkpoint"]
        self.export_weights = config["trainer"]["export"]
        self.metrics = config["trainer"]["metrics"]
        logger = config["trainer"]["log"]

        if logger:
            self.logger = Logger(logger)

        if pretrained:
            assert config["pretrain"], "pretrain weight is required"
            state_dict = torch.load(config["pretrain"], map_location=self.device)
            self.model.load_state_dict(state_dict, strict=True)

        self.slim_mode = config["slim"]["mode"]
        if self.slim_mode == "prune":
            self._initialize_prune_step()

        transforms = None
        if self.image_aug:
            transforms = augmentor

        self.train_gen = self.data_gen(
            "train_{}".format(self.dataset_name),
            self.data_root,
            self.train_annotation,
            self.masked_language_model,
            transform=transforms,
        )
        if self.valid_annotation:
            self.valid_gen = self.data_gen(
                "valid_{}".format(self.dataset_name), self.data_root, self.valid_annotation, masked_language_model=False
            )

        self.iter = 0

        self.optimizer = AdamW(self.model.parameters(), betas=(0.9, 0.98), eps=1e-09)
        self.scheduler = CosineAnnealingLR(
            self.optimizer,
            **config["optimizer"],
        )

        self.criterion = LabelSmoothingLoss(len(self.vocab), padding_idx=self.vocab.pad, smoothing=0.1)
        self.train_losses = []

    def train(self):
        total_loss = 0

        total_loader_time = 0
        total_gpu_time = 0
        best_acc = 0

        start = time.time()
        for epoch in range(1, self.num_epochs + 1):

            for batch in self.train_gen:
                total_loader_time += time.time() - start

                loss = self.step(batch)
                total_loss += loss
                self.train_losses.append((self.iter, loss))

                if self.iter % self.print_every == 0:
                    info = (
                        "iter: {:06d} - train loss: {:.3f} - lr: {:.2e} - load time: {:.2f} - gpu time: {:.2f}".format(
                            self.iter,
                            total_loss / self.print_every,
                            self.optimizer.param_groups[0]["lr"],
                            total_loader_time,
                            total_gpu_time,
                        )
                    )

                    total_loss = 0
                    total_loader_time = 0
                    total_gpu_time = 0
                    print(info)
                    self.logger.log(info)

                if self.valid_annotation and self.iter % self.valid_every == 0:
                    val_loss = self.validate()
                    acc_full_seq, acc_per_char = self.precision(self.metrics)

                    info = ("epoch: {:02d} - iter: {:06d} - valid loss: {:.3f} - acc full seq: {:.4f} - acc per char: "
                            "{:.4f}").format(
                        epoch,
                        self.iter,
                        val_loss,
                        acc_full_seq,
                        acc_per_char,
                    )
                    print(info)
                    self.logger.log(info)

                    if acc_full_seq > best_acc:
                        self.save_weights(self.export_weights)
                        best_acc = acc_full_seq

                self.iter += 1

            self.scheduler.step()

    def validate(self):
        self.model.eval()

        total_loss = []

        with torch.no_grad():
            for step, batch in enumerate(self.valid_gen):
                batch = self.batch_to_device(batch)
                img, tgt_input, tgt_output, tgt_padding_mask = (
                    batch["img"],
                    batch["tgt_input"],
                    batch["tgt_output"],
                    batch["tgt_padding_mask"],
                )

                outputs = self.model(img, tgt_input, tgt_padding_mask)
                outputs = outputs.flatten(0, 1)

                tgt_output = tgt_output.flatten()
                loss = self.criterion(outputs, tgt_output)

                total_loss.append(loss.item())

                del outputs
                del loss

        total_loss = np.mean(total_loss)
        self.model.train()

        return total_loss

    def predict(self, sample=None):
        pred_sents = []
        actual_sents = []
        img_files = []

        for batch in self.valid_gen:
            batch = self.batch_to_device(batch)

            if self.beamsearch:
                translated_sentence = batch_translate_beam_search(batch["img"], self.model)
                prob = None
            else:
                translated_sentence, prob = translate(batch["img"], self.model)

            pred_sent = self.vocab.batch_decode(translated_sentence.tolist())
            actual_sent = self.vocab.batch_decode(batch["tgt_output"].tolist())

            img_files.extend(batch["filenames"])

            pred_sents.extend(pred_sent)
            actual_sents.extend(actual_sent)

            if sample is not None and len(pred_sents) > sample:
                break

        return pred_sents, actual_sents, img_files, prob

    def precision(self, sample=None):

        pred_sents, actual_sents, _, _ = self.predict(sample=sample)

        acc_full_seq = compute_accuracy(actual_sents, pred_sents, mode="full_sequence")
        acc_per_char = compute_accuracy(actual_sents, pred_sents, mode="per_char")

        return acc_full_seq, acc_per_char

    def visualize_prediction(self, sample=16, errorcase=False, fontname="serif", fontsize=16):

        pred_sents, actual_sents, img_files, probs = self.predict(sample)

        if errorcase:
            wrongs = []
            for i in range(len(img_files)):
                if pred_sents[i] != actual_sents[i]:
                    wrongs.append(i)

            pred_sents = [pred_sents[i] for i in wrongs]
            actual_sents = [actual_sents[i] for i in wrongs]
            img_files = [img_files[i] for i in wrongs]
            probs = [probs[i] for i in wrongs]

        img_files = img_files[:sample]

        fontdict = {"family": fontname, "size": fontsize}

        for vis_idx in range(0, len(img_files)):
            img_path = img_files[vis_idx]
            pred_sent = pred_sents[vis_idx]
            actual_sent = actual_sents[vis_idx]
            prob = probs[vis_idx]

            img = Image.open(open(img_path, "rb"))
            plt.figure()
            plt.imshow(img)
            plt.title(
                "prob: {:.3f} - pred: {} - actual: {}".format(prob, pred_sent, actual_sent),
                loc="left",
                fontdict=fontdict,
            )
            plt.axis("off")

        plt.show()

    def visualize_dataset(self, sample=16, fontname="serif"):
        n = 0
        for batch in self.train_gen:
            for i in range(self.batch_size):
                img = batch["img"][i].numpy().transpose(1, 2, 0)
                sent = self.vocab.decode(batch["tgt_input"].T[i].tolist())

                plt.figure()
                plt.title("sent: {}".format(sent), loc="center", fontname=fontname)
                plt.imshow(img)
                plt.axis("off")

                n += 1
                if n >= sample:
                    plt.show()
                    return

    def load_checkpoint(self, filename):
        checkpoint = torch.load(filename)

        optim = ScheduledOptim(
            Adam(self.model.parameters(), betas=(0.9, 0.98), eps=1e-09),
            self.config["transformer"]["d_model"],
            **self.config["optimizer"],
        )

        self.optimizer.load_state_dict(checkpoint["optimizer"])
        self.model.load_state_dict(checkpoint["state_dict"])
        self.iter = checkpoint["iter"]

        self.train_losses = checkpoint["train_losses"]

    def save_checkpoint(self, filename):
        state = {
            "iter": self.iter,
            "state_dict": self.model.state_dict(),
            "optimizer": self.optimizer.state_dict(),
            "train_losses": self.train_losses,
        }

        path, _ = os.path.split(filename)
        os.makedirs(path, exist_ok=True)

        torch.save(state, filename)

    def save_weights(self, filename):
        path, _ = os.path.split(filename)
        os.makedirs(path, exist_ok=True)

        torch.save(self.model.state_dict(), filename)

    def batch_to_device(self, batch):
        img = batch["img"].to(self.device, non_blocking=True)
        tgt_input = batch["tgt_input"].to(self.device, non_blocking=True)
        tgt_output = batch["tgt_output"].to(self.device, non_blocking=True)
        tgt_padding_mask = batch["tgt_padding_mask"].to(self.device, non_blocking=True)

        batch = {
            "img": img,
            "tgt_input": tgt_input,
            "tgt_output": tgt_output,
            "tgt_padding_mask": tgt_padding_mask,
            "filenames": batch["filenames"],
        }

        return batch

    def data_gen(self, lmdb_path, data_root, annotation, masked_language_model=True, transform=None):
        dataset = OCRDataset(
            lmdb_path=lmdb_path,
            root_dir=data_root,
            annotation_path=annotation,
            vocab=self.vocab,
            transform=transform,
            image_height=self.config["dataset"]["image_height"],
            image_min_width=self.config["dataset"]["image_min_width"],
            image_max_width=self.config["dataset"]["image_max_width"],
        )

        sampler = ClusterRandomSampler(dataset, self.batch_size, True)
        collate_fn = Collator(masked_language_model)

        gen = DataLoader(
            dataset,
            batch_size=self.batch_size,
            sampler=sampler,
            collate_fn=collate_fn,
            shuffle=False,
            drop_last=False,
            **self.config["dataloader"],
        )

        return gen

    def data_gen_v1(self, lmdb_path, data_root, annotation):
        data_gen = DataGen(
            data_root,
            annotation,
            self.vocab,
            "cpu",
            image_height=self.config["dataset"]["image_height"],
            image_min_width=self.config["dataset"]["image_min_width"],
            image_max_width=self.config["dataset"]["image_max_width"],
        )

        return data_gen

    def step(self, batch):
        self.model.train()

        batch = self.batch_to_device(batch)
        self.optimizer.zero_grad()

        img, tgt_input, tgt_output, tgt_padding_mask = (
            batch["img"],
            batch["tgt_input"],
            batch["tgt_output"],
            batch["tgt_padding_mask"],
        )

        outputs = self.model(img, tgt_input, tgt_key_padding_mask=tgt_padding_mask)
        outputs = outputs.view(-1, outputs.size(2))  # flatten(0, 1)
        tgt_output = tgt_output.view(-1)  # flatten()

        loss = self.criterion(outputs, tgt_output)
        loss.backward()

        torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1)

        self.optimizer.step()

        loss_item = loss.item()

        return loss_item

    def _initialize_prune_step(self):
        img = torch.randn(32, 3, 32, 300).to(self.device)
        tgt_input = torch.randint(0, len(self.vocab), (93, 32)).to(self.device)
        tgt_padding_mask = torch.BoolTensor(torch.randn(32, 93) < 0.05).to(self.device)
        example_inputs = (img, tgt_input, tgt_padding_mask)

        print("Before pruning")
        base_macs, base_params = tp.utils.count_ops_and_params(self.model, example_inputs)
        print(f"{base_macs=}, {base_params=}")

        for p in self.model.parameters():
            p.requires_grad_(True)

        ignored_layers = []
        channel_groups = {}
        unwrapped_parameters = None

        for m in self.model.modules():
            if isinstance(m, nn.Linear) and m.out_features == len(self.vocab):
                ignored_layers.append(m)

            if isinstance(m, nn.MultiheadAttention):
                channel_groups[m] = m.num_heads

        imp = tp.importance.MagnitudeImportance(p=1)
        pruner = tp.pruner.MagnitudePruner(
            self.model,
            example_inputs,
            iterative_steps=1,
            pruning_ratio=0.7,
            global_pruning=True,
            round_to=None,
            unwrapped_parameters=unwrapped_parameters,
            ignored_layers=ignored_layers,
            channel_groups=channel_groups,
            importance=imp,
        )

        layer_channel_cfg = {}
        for module in self.model.modules():
            if module not in pruner.ignored_layers:
                if isinstance(module, nn.Conv2d):
                    layer_channel_cfg[module] = module.out_channels
                elif isinstance(module, nn.Linear):
                    layer_channel_cfg[module] = module.out_features

        for g in pruner.step(interactive=True):
            g.prune()

        base_macs_after_pruning, base_params_after_pruning = tp.utils.count_ops_and_params(self.model, example_inputs)
        print("After pruning")
        print(f"{base_macs_after_pruning=}, {base_params_after_pruning=}")