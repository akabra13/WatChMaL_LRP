"""
Class for training a fully supervised classifier
"""

# hydra imports
from hydra.utils import instantiate

# torch imports
import torch
from torch import optim
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parallel import DistributedDataParallel as DDP

# generic imports
from math import floor, ceil
import numpy as np
from numpy import savez
import os
from time import strftime, localtime, time
import sys
from sys import stdout
import copy

# WatChMaL imports
from watchmal.dataset.data_utils import get_data_loader
from watchmal.utils.logging_utils import CSVData

#Zennit imports
from zennit.attribution import Gradient, SmoothGrad
from zennit.core import Stabilizer
from zennit.composites import EpsilonGammaBox, EpsilonPlusFlat, EpsilonAlpha2Beta1
from zennit.composites import SpecialFirstLayerMapComposite, NameMapComposite
from zennit.image import imgify, imsave
from zennit.rules import Epsilon, ZPlus, ZBox, Norm, Pass, Flat, AlphaBeta
from zennit.types import Convolution, Activation, AvgPool, Linear as AnyLinear
from zennit.types import BatchNorm, MaxPool
from zennit.torchvision import VGGCanonizer, ResNetCanonizer

class ClassifierEngine:
    """Engine for performing training or evaluation  for a classification network."""
    def __init__(self, model, rank, gpu, dump_path, label_set=None):
        """
        Parameters
        ==========
        model
            nn.module object that contains the full network that the engine will use in training or evaluation.
        rank : int
            The rank of process among all spawned processes (in multiprocessing mode).
        gpu : int
            The gpu that this process is running on.
        dump_path : string
            The path to store outputs in.
        label_set : sequence
            The set of possible labels to classify (if None, which is the default, then class labels in the data must be
            0 to N).
        """
        # create the directory for saving the log and dump files
        self.epoch = 0.
        self.step = 0
        self.best_validation_loss = 1.0e10
        self.dirpath = dump_path
        self.rank = rank
        self.model = model
        self.device = torch.device(gpu)

        # Setup the parameters to save given the model type
        if isinstance(self.model, DDP):
            self.is_distributed = True
            self.model_accs = self.model.module
            self.ngpus = torch.distributed.get_world_size()
        else:
            self.is_distributed = False
            self.model_accs = self.model

        self.data_loaders = {}
        self.label_set = label_set

        # define the placeholder attributes
        self.data = None
        self.labels = None
        self.loss = None

        # logging attributes
        self.train_log = CSVData(self.dirpath + "log_train_{}.csv".format(self.rank))

        if self.rank == 0:
            self.val_log = CSVData(self.dirpath + "log_val.csv")

        self.criterion = nn.CrossEntropyLoss()
        self.softmax = nn.Softmax(dim=1)
        
        self.optimizer = None
        self.scheduler = None

    def epsilonAlpha2Beta1(self, model, data):
        # use the ResNet-specific canonizer
        canonizer = ResNetCanonizer()
        
        # create a composite, specifying the canonizers
        composite = EpsilonAlpha2Beta1(canonizers=[canonizer])
        
        total_output = []
        total_relevance = []
        #iterate over all the possible classes
        for i in range(4):
            
            # choose a target class for the attribution
            initial_target = torch.eye(4)[[i]]
            target = torch.tile(initial_target, (data.shape[0],1))
            
            # create the attributor, specifying model and composite
            with Gradient(model=model, composite=composite) as attributor:
                # compute the model output and attribution
                print(data.shape)
                output, attribution = attributor(data, target)
                
            relevance = attribution.sum(0)
            total_output.extend(output.detach().cpu().numpy())
            total_relevance.extend(relevance.detach().cpu().numpy())
        
        return np.array(total_output), np.array(total_relevance)
    
    def epsilonPlusFlat(self, model, data):
        # use the ResNet-specific canonizer
        canonizer = ResNetCanonizer()
        
        # create a composite, specifying the canonizers
        composite = EpsilonPlusFlat(canonizers=[canonizer])
        
        total_output = []
        total_relevance = []
        #iterate over all the possible classes
        for i in range(4):
            
            # choose a target class for the attribution
            target = torch.eye(4)[[i]]
            
            # create the attributor, specifying model and composite
            with Gradient(model=model, composite=composite) as attributor:
                # compute the model output and attribution
                output, attribution = attributor(data, target)
            
            relevance = attribution.sum(0)
            total_output.extend(output.detach().cpu().numpy())
            total_relevance.extend(relevance.detach().cpu().numpy())
        
        return np.array(total_output), np.array(total_relevance)
    
    def configure_optimizers(self, optimizer_config):
        """Instantiate an optimizer from a hydra config."""
        self.optimizer = instantiate(optimizer_config, params=self.model_accs.parameters())

    def configure_scheduler(self, scheduler_config):
        """Instantiate a scheduler from a hydra config."""
        self.scheduler = instantiate(scheduler_config, optimizer=self.optimizer)
        print('Successfully set up Scheduler')


    def configure_data_loaders(self, data_config, loaders_config, is_distributed, seed):
        """
        Set up data loaders from loaders hydra configs for the data config, and a list of data loader configs.

        Parameters
        ==========
        data_config
            Hydra config specifying dataset.
        loaders_config
            Hydra config specifying a list of dataloaders.
        is_distributed : bool
            Whether running in multiprocessing mode.
        seed : int
            Random seed to use to initialize dataloaders.
        """
        for name, loader_config in loaders_config.items():
            self.data_loaders[name] = get_data_loader(**data_config, **loader_config, is_distributed=is_distributed, seed=seed)
            if self.label_set is not None:
                self.data_loaders[name].dataset.map_labels(self.label_set)
    
    def get_synchronized_metrics(self, metric_dict):
        """
        Gathers metrics from multiple processes using pytorch distributed operations for DistributedDataParallel

        Parameters
        ==========
        metric_dict : dict of torch.Tensor
            Dictionary containing values that are tensor outputs of a single process.
        
        Returns
        =======
        global_metric_dict : dict of torch.Tensor
            Dictionary containing concatenated list of tensor values gathered from all processes
        """
        global_metric_dict = {}
        for name, array in zip(metric_dict.keys(), metric_dict.values()):
            tensor = torch.as_tensor(array).to(self.device)
            global_tensor = [torch.zeros_like(tensor).to(self.device) for i in range(self.ngpus)]
            torch.distributed.all_gather(global_tensor, tensor)
            global_metric_dict[name] = torch.cat(global_tensor)
        
        return global_metric_dict

    def forward(self, train=True):
        """
        Compute predictions and metrics for a batch of data.

        Parameters
        ==========
        train : bool
            Whether in training mode, requiring computing gradients for backpropagation

        Returns
        =======
        dict
            Dictionary containing loss, predicted labels, softmax, accuracy, and raw model outputs
        """
        with torch.set_grad_enabled(train):
            # Move the data and the labels to the GPU (if using CPU this has no effect)
            data = self.data.to(self.device)
            labels = self.labels.to(self.device)

            model_out = self.model(data)
            
            softmax = self.softmax(model_out)
            predicted_labels = torch.argmax(model_out, dim=-1)

            result = {'predicted_labels': predicted_labels,
                      'softmax': softmax,
                      'raw_pred_labels': model_out}

            self.loss = self.criterion(model_out, labels)
            accuracy = (predicted_labels == labels).sum().item() / float(predicted_labels.nelement())

            result['loss'] = self.loss.item()
            result['accuracy'] = accuracy
        
        return result
    
    def backward(self):
        """Backward pass using the loss computed for a mini-batch"""
        self.optimizer.zero_grad()  # reset accumulated gradient
        self.loss.backward()        # compute new gradient
        self.optimizer.step()       # step params

    def train(self, train_config):
        """
        Train the model on the training set.

        Parameters
        ==========
        train_config
            Hydra config specifying training parameters
        """
        # initialize training params
        epochs              = train_config.epochs
        report_interval     = train_config.report_interval
        val_interval        = train_config.val_interval
        num_val_batches     = train_config.num_val_batches
        checkpointing       = train_config.checkpointing
        save_interval = train_config.save_interval if 'save_interval' in train_config else None

        # set the iterations at which to dump the events and their metrics
        if self.rank == 0:
            print(f"Training... Validation Interval: {val_interval}")

        # set model to training mode
        self.model.train()

        # initialize epoch and iteration counters
        self.epoch = 0.
        self.iteration = 0
        self.step = 0
        # keep track of the validation loss
        self.best_validation_loss = 1.0e10

        # initialize the iterator over the validation set
        val_iter = iter(self.data_loaders["validation"])

        # global training loop for multiple epochs
        for self.epoch in range(epochs):
            if self.rank == 0:
                print('Epoch', self.epoch+1, 'Starting @', strftime("%Y-%m-%d %H:%M:%S", localtime()))
            
            times = []

            start_time = time()
            iteration_time = start_time

            train_loader = self.data_loaders["train"]
            self.step = 0
            # update seeding for distributed samplers
            if self.is_distributed:
                train_loader.sampler.set_epoch(self.epoch)

            # local training loop for batches in a single epoch 
            for self.step, train_data in enumerate(train_loader):
                
                # run validation on given intervals
                if self.iteration % val_interval == 0:
                    self.validate(val_iter, num_val_batches, checkpointing)
                
                # Train on batch
                self.data = train_data['data']
                self.labels = train_data['labels']

                # Call forward: make a prediction & measure the average error using data = self.data
                res = self.forward(True)

                #Call backward: backpropagate error and update weights using loss = self.loss
                self.backward()

                # update the epoch and iteration
                # self.epoch += 1. / len(self.data_loaders["train"])
                self.step += 1
                self.iteration += 1
                
                # get relevant attributes of result for logging
                train_metrics = {"iteration": self.iteration, "epoch": self.epoch, "loss": res["loss"], "accuracy": res["accuracy"]}
                
                # record the metrics for the mini-batch in the log
                self.train_log.record(train_metrics)
                self.train_log.write()
                self.train_log.flush()
                
                # print the metrics at given intervals
                if self.rank == 0 and self.iteration % report_interval == 0:
                    previous_iteration_time = iteration_time
                    iteration_time = time()

                    print("... Iteration %d ... Epoch %d ... Step %d/%d  ... Training Loss %1.3f ... Training Accuracy %1.3f ... Time Elapsed %1.3f ... Iteration Time %1.3f" %
                          (self.iteration, self.epoch+1, self.step, len(train_loader), res["loss"], res["accuracy"], iteration_time - start_time, iteration_time - previous_iteration_time))
            
            if self.scheduler is not None:
                self.scheduler.step()

            if (save_interval is not None) and ((self.epoch+1)%save_interval == 0):
                self.save_state(name=f'_epoch_{self.epoch+1}')   
      
        self.train_log.close()
        if self.rank == 0:
            self.val_log.close()

    def validate(self, val_iter, num_val_batches, checkpointing):
        """
        Perform validation with the current state, on a number of batches of the validation set.

        Parameters
        ----------
        val_iter : iter
            Iterator of the validation dataset.
        num_val_batches : int
            Number of validation batches to iterate over.
        checkpointing : bool
            Whether to save the current state to disk.
        """
        # set model to eval mode
        self.model.eval()
        val_metrics = {"iteration": self.iteration, "loss": 0., "accuracy": 0., "saved_best": 0}
        for val_batch in range(num_val_batches):
            try:
                val_data = next(val_iter)
            except StopIteration:
                del val_iter
                print("Fetching new validation iterator...")
                val_iter = iter(self.data_loaders["validation"])
                val_data = next(val_iter)

            # extract the event data from the input data tuple
            self.data = val_data['data']
            self.labels = val_data['labels']

            val_res = self.forward(False)

            val_metrics["loss"] += val_res["loss"]
            val_metrics["accuracy"] += val_res["accuracy"]
        # return model to training mode
        self.model.train()
        # record the validation stats
        val_metrics["loss"] /= num_val_batches
        val_metrics["accuracy"] /= num_val_batches
        local_val_metrics = {"loss": np.array([val_metrics["loss"]]), "accuracy": np.array([val_metrics["accuracy"]])}

        if self.is_distributed:
            global_val_metrics = self.get_synchronized_metrics(local_val_metrics)
            for name, tensor in zip(global_val_metrics.keys(), global_val_metrics.values()):
                global_val_metrics[name] = np.array(tensor.cpu())
        else:
            global_val_metrics = local_val_metrics

        if self.rank == 0:
            # Save if this is the best model so far
            global_val_loss = np.mean(global_val_metrics["loss"])
            global_val_accuracy = np.mean(global_val_metrics["accuracy"])

            val_metrics["loss"] = global_val_loss
            val_metrics["accuracy"] = global_val_accuracy
            val_metrics["epoch"] = self.epoch

            if val_metrics["loss"] < self.best_validation_loss:
                self.best_validation_loss = val_metrics["loss"]
                print('best validation loss so far!: {}'.format(self.best_validation_loss))
                self.save_state("BEST")
                val_metrics["saved_best"] = 1

            # Save the latest model if checkpointing
            if checkpointing:
                self.save_state()

            self.val_log.record(val_metrics)
            self.val_log.write()
            self.val_log.flush()

    def evaluate(self, test_config):
        """Evaluate the performance of the trained model on the test set."""
        print("evaluating in directory: ", self.dirpath)

        
        # Variables to output at the end
        eval_loss = 0.0
        eval_acc = 0.0
        eval_iterations = 0
        
        # Iterate over the validation set to calculate val_loss and val_acc
        with torch.enable_grad():
            
            # Set the model to evaluation mode
            current_model = self.model.eval()
            
            # Variables for the confusion matrix
            loss, accuracy, indices, labels, predictions, softmaxes= [],[],[],[],[],[]
            
            # Variables for LRP
            lrp_output = []
            relevance = []
            
            # Extract the event data and label from the DataLoader iterator
            for it, eval_data in enumerate(self.data_loaders["test"]):
                
                # load data
                self.data = eval_data['data']
                self.labels = eval_data['labels']

                eval_indices = eval_data['indices']
                
                # Run the forward procedure and output the result
                result = self.forward(train=False)
                
                data = self.data.to(self.device)

                # Conduct the LRP algorithm and add it to the final result
                lrp_output.extend([[]])
                relevance.extend([[]])
                for i in range(4):
                    #for j in range(self.data.shape[0]):
                        #print(self.data.shape)
                        #print(current_model)
                        #print(i)
                        #sliced_data = data[j, :, :, :]
                    lrp_output, attribution = self.epsilonAlpha2Beta1(current_model, data)
                    lrp_output[eval_iterations].extend(lrp_output)
                    relevance[eval_iterations].extend(attribution)

                eval_loss += result['loss']
                eval_acc  += result['accuracy']
                
                # Add the local result to the final result
                indices.extend(eval_indices.numpy())
                labels.extend(self.labels.numpy())
                predictions.extend(result['predicted_labels'].detach().cpu().numpy())
                softmaxes.extend(result["softmax"].detach().cpu().numpy())
           
                print("eval_iteration : " + str(it) + " eval_loss : " + str(result["loss"]) + " eval_accuracy : " + str(result["accuracy"]))
            
                eval_iterations += 1
        
        # convert arrays to torch tensors
        print("loss : " + str(eval_loss/eval_iterations) + " accuracy : " + str(eval_acc/eval_iterations))

        iterations = np.array([eval_iterations])
        loss = np.array([eval_loss])
        accuracy = np.array([eval_acc])

        local_eval_metrics_dict = {"eval_iterations":iterations, "eval_loss":loss, "eval_acc":accuracy}
        
        indices     = np.array(indices)
        labels      = np.array(labels)
        predictions = np.array(predictions)
        softmaxes   = np.array(softmaxes)
        lrp_output  = np.array(lrp_output)
        relevance   = np.array(relevance)
        
        local_eval_results_dict = {"indices":indices, "labels":labels, "predictions":predictions, "softmaxes":softmaxes, "lrp_output":lrp_output, "relevance":relevance}

        if self.is_distributed:
            # Gather results from all processes
            global_eval_metrics_dict = self.get_synchronized_metrics(local_eval_metrics_dict)
            global_eval_results_dict = self.get_synchronized_metrics(local_eval_results_dict)
            
            if self.rank == 0:
                for name, tensor in zip(global_eval_metrics_dict.keys(), global_eval_metrics_dict.values()):
                    local_eval_metrics_dict[name] = np.array(tensor.cpu())
                
                indices     = np.array(global_eval_results_dict["indices"].cpu())
                labels      = np.array(global_eval_results_dict["labels"].cpu())
                predictions = np.array(global_eval_results_dict["predictions"].cpu())
                softmaxes   = np.array(global_eval_results_dict["softmaxes"].cpu())
                lrp_output  = np.array(global_eval_results_dict["lrp_output"].cpu())
                relevance   = np.array(global_eval_results_dict["relevance"].cpu())
        
        if self.rank == 0:
#            print("Sorting Outputs...")
#            sorted_indices = np.argsort(indices)

            # Save overall evaluation results
            print("Saving Data...")
            np.save(self.dirpath + "indices.npy", indices)#sorted_indices)
            np.save(self.dirpath + "labels.npy", labels)#[sorted_indices])
            np.save(self.dirpath + "predictions.npy", predictions)#[sorted_indices])
            np.save(self.dirpath + "softmax.npy", softmaxes)#[sorted_indices])
            np.save(self.dirpath + "lrp_output.npy", lrp_output)
            np.save(self.dirpath + "relevance.npy", relevance)

            # Compute overall evaluation metrics
            val_iterations = np.sum(local_eval_metrics_dict["eval_iterations"])
            val_loss = np.sum(local_eval_metrics_dict["eval_loss"])
            val_acc = np.sum(local_eval_metrics_dict["eval_acc"])

            print("\nAvg eval loss : " + str(val_loss/val_iterations),
                  "\nAvg eval acc : "  + str(val_acc/val_iterations))
        
    # ========================================================================
    # Saving and loading models

    def save_state(self, name=""):
        """
        Save model weights and other training state information to a file.
        
        Parameters
        ==========
        name
            Suffix for the filename. Should be "BEST" for saving the best validation state.
        
        Returns
        =======
        filename : string
            Filename where the saved state is saved.
        """
        filename = "{}{}{}{}".format(self.dirpath,
                                     str(self.model._get_name()),
                                     name,
                                     ".pth")
        
        # Save model state dict in appropriate from depending on number of gpus
        model_dict = self.model_accs.state_dict()
        
        # Save parameters
        # 0+1) iteration counter + optimizer state => in case we want to "continue training" later
        # 2) network weight
        torch.save({
            'global_step': self.iteration,
            'optimizer': self.optimizer.state_dict(),
            'state_dict': model_dict
        }, filename)
        print('Saved checkpoint as:', filename)
        return filename

    def restore_best_state(self, placeholder):
        """Restore model using best model found in current directory."""
        best_validation_path = "{}{}{}{}".format(self.dirpath,
                                     str(self.model._get_name()),
                                     "BEST",
                                     ".pth")

        self.restore_state_from_file(best_validation_path)
    
    def restore_state(self, restore_config):
        """Restore model and training state from a file given in the `weight_file` entry of the config."""
        self.restore_state_from_file(restore_config.weight_file)

    def restore_state_from_file(self, weight_file):
        """Restore model and training state from a given filename."""
        # Open a file in read-binary mode
        with open(weight_file, 'rb') as f:
            print('Restoring state from', weight_file)

            # torch interprets the file, then we can access using string keys
            checkpoint = torch.load(f)
            
            # load network weights
            self.model_accs.load_state_dict(checkpoint['state_dict'])
            
            # if optim is provided, load the state of the optim
            if self.optimizer is not None:
                self.optimizer.load_state_dict(checkpoint['optimizer'])
            
            # load iteration count
            self.iteration = checkpoint['global_step']
        
        print('Restoration complete.')
