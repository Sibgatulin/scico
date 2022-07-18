# -*- coding: utf-8 -*-
# Copyright (C) 2021-2022 by SCICO Developers
# All rights reserved. BSD 3-clause License.
# This file is part of the SCICO package. Details of the copyright and
# user license can be found in the 'LICENSE' file distributed with the
# package.

"""Utilities for training Flax models.

Assummes sharded batched data and data parallel training.
"""

import functools
import os
import time
from typing import Any, Callable, List, Optional, Tuple, TypedDict, Union

import jax
import jax.numpy as jnp
from jax import lax

import optax

from flax import jax_utils
from flax.core import freeze, unfreeze
from flax.training import common_utils, train_state
from flax.traverse_util import ModelParamTraversal

try:
    from tensorflow.io import gfile  # noqa: F401
except ImportError:
    have_tf = False
else:
    have_tf = True

if have_tf:
    from flax.training import checkpoints

from scico.diagnostics import IterationStats
from scico.flax import create_input_iter
from scico.flax.train.clu_utils import get_parameter_overview
from scico.flax.train.input_pipeline import DataSetDict
from scico.metric import snr
from scico.typing import Array, Shape

ModuleDef = Any
KeyArray = Union[Array, jax._src.prng.PRNGKeyArray]
PyTree = Any


class ConfigDict(TypedDict):
    """Dictionary structure for training parmeters.

    Definition of the dictionary structure
    expected for specifying training parameters."""

    seed: float
    depth: int
    num_filters: int
    block_depth: int
    opt_type: str
    momentum: float
    batch_size: int
    num_epochs: int
    base_learning_rate: float
    lr_decay_rate: float
    warmup_epochs: int
    num_train_steps: int
    steps_per_eval: int
    log_every_steps: int
    steps_per_epoch: int
    cg_iter: int


class ModelVarDict(TypedDict):
    """Dictionary structure for Flax variables.

    Definition of the dictionary structure
    grouping all Flax model variables."""

    params: PyTree
    batch_stats: PyTree


class MetricsDict(TypedDict, total=False):
    """Dictionary structure for training metrics.

    Definition of the dictionary structure
    for metrics computed or updates made during
    training."""

    loss: float
    snr: float
    learning_rate: float


# Loss Function
def mse_loss(output: Array, labels: Array) -> float:
    """
    Compute Mean Squared Error (MSE) loss for training
    via Optax.

    Args:
        output: Comparison signal.
        labels: Reference signal.

    Returns:
        MSE between `output` and `labels`.
    """
    mse = optax.l2_loss(output, labels)
    return jnp.mean(mse)


def compute_metrics(output: Array, labels: Array, criterion: Callable = mse_loss) -> MetricsDict:
    """Compute diagnostic metrics. Assummes sharded batched
    data (i.e. it only works inside pmap because it needs an
    axis name).

    Args:
        output: Comparison signal.
        labels: Reference signal.
        criterion: Loss function. Default: :meth:`mse_loss`.

    Returns:
        Loss and SNR between `output` and `labels`.
    """
    loss = criterion(output, labels)
    snr_ = snr(labels, output)
    metrics: MetricsDict = {
        "loss": loss,
        "snr": snr_,
    }
    metrics = lax.pmean(metrics, axis_name="batch")
    return metrics


# Learning rate
def create_cnst_lr_schedule(config: ConfigDict) -> optax._src.base.Schedule:
    """Create learning rate to be a constant specified
    value.

    Args:
        config: Dictionary of configuration. The value
           to use corresponds to the `base_learning_rate`
           keyword.

    Returns:
        schedule: A function that maps step counts to values.
    """
    schedule = optax.constant_schedule(config["base_learning_rate"])
    return schedule


def create_exp_lr_schedule(config: ConfigDict) -> optax._src.base.Schedule:
    """Create learning rate schedule to have an exponential decay.

    Args:
        config: Dictionary of configuration. The values to use correspond to `base_learning_rate`,
            `num_epochs`, `steps_per_epochs` and `lr_decay_rate`.

    Returns:
        schedule: A function that maps step counts to values.
    """
    decay_steps = config["num_epochs"] * config["steps_per_epoch"]
    schedule = optax.exponential_decay(
        config["base_learning_rate"], decay_steps, config["lr_decay_rate"]
    )
    return schedule


def create_cosine_lr_schedule(config: ConfigDict) -> optax._src.base.Schedule:
    """Create learning rate to follow a pre-specified
    schedule with warmup and cosine stages.

    Args:
        config: Dictionary of configuration. The parameters
        to use correspond to keywords: `base_learning_rate`,
        `num_epochs`, `warmup_epochs` and `steps_per_epoch`.

    Returns:
        schedule: A function that maps step counts to values.
    """
    # Warmup stage
    warmup_fn = optax.linear_schedule(
        init_value=0.0,
        end_value=config["base_learning_rate"],
        transition_steps=config["warmup_epochs"] * config["steps_per_epoch"],
    )
    # Cosine stage
    cosine_epochs = max(config["num_epochs"] - config["warmup_epochs"], 1)
    cosine_fn = optax.cosine_decay_schedule(
        init_value=config["base_learning_rate"],
        decay_steps=cosine_epochs * config["steps_per_epoch"],
    )

    schedule = optax.join_schedules(
        schedules=[warmup_fn, cosine_fn],
        boundaries=[config["warmup_epochs"] * config["steps_per_epoch"]],
    )

    return schedule


def initialize(key: KeyArray, model: ModuleDef, ishape: Shape) -> Tuple[PyTree, ...]:
    """Initialize Flax model.

    Args:
        key: A PRNGKey used as the random key.
        model: Flax model to train.
        ishape: Shape of signal (image) to process by `model`.

    Returns:
        Initial model parameters (including `batch_stats`).
    """
    input_shape = (1, ishape[0], ishape[1], model.channels)

    @jax.jit
    def init(*args):
        return model.init(*args)

    variables = init({"params": key}, jnp.ones(input_shape, model.dtype))
    return variables["params"], variables["batch_stats"]


# Flax Train State
class TrainState(train_state.TrainState):
    """Definition of Flax train state including
    `batch_stats` for batch normalization."""

    batch_stats: Any


def create_train_state(
    key: KeyArray,
    config: ConfigDict,
    model: ModuleDef,
    ishape: Shape,
    learning_rate_fn: optax._src.base.Schedule,
    variables0: Optional[ModelVarDict] = None,
) -> TrainState:
    """Create initial training state.

    Args:
        key: A PRNGKey used as the random key.
        config: Dictionary of configuration. The values
           to use correspond to keywords: `opt_type`
           and `momentum`.
        model: Flax model to train.
        ishape: Shape of signal (image) to process by `model`.
        variables0: Optional initial state of model
           parameters. If not provided a random initialization
           is performed. Default: ``None``.
        learning_rate_fn: A function that maps step
           counts to values.

    Returns:
        state: Flax train state which includes the
           model apply function, the model parameters
           and an Optax optimizer.
    """
    if variables0 is None:
        params, batch_stats = initialize(key, model, ishape)
    else:
        params = variables0["params"]
        batch_stats = variables0["batch_stats"]

    if config["opt_type"] == "SGD":
        # Stochastic Gradient Descent optimiser
        tx = optax.sgd(learning_rate=learning_rate_fn, momentum=config["momentum"], nesterov=True)
    elif config["opt_type"] == "ADAM":
        # Adam optimiser
        tx = optax.adam(
            learning_rate=learning_rate_fn,
        )
    elif config["opt_type"] == "ADAMW":
        # Adam with weight decay regularization
        tx = optax.adamw(
            learning_rate=learning_rate_fn,
        )
    else:
        raise NotImplementedError(
            f"Optimizer specified {config['opt_type']} has not been included in SCICO"
        )

    state = TrainState.create(
        apply_fn=model.apply,
        params=params,
        tx=tx,
        batch_stats=batch_stats,
    )

    return state


# Flax checkpoints
def restore_checkpoint(
    state: TrainState, workdir: Union[str, os.PathLike]
) -> TrainState:  # pragma: no cover
    """Load model and optimiser state.

    Args:
        state: Flax train state which includes model and
            optimiser parameters.
        workdir: checkpoint file or directory of checkpoints
            to restore from.

    Returns:
        Restored `state` updated from checkpoint file,
        or if no checkpoint files present, returns the
        passed-in `state` unchanged.
    """
    return checkpoints.restore_checkpoint(workdir, state)


def save_checkpoint(state: TrainState, workdir: Union[str, os.PathLike]):  # pragma: no cover
    """Store model and optimiser state.

    Args:
        state: Flax train state which includes model and
            optimiser parameters.
        workdir: str or pathlib-like path to store checkpoint
            files in.
    """
    if jax.process_index() == 0:
        # get train state from first replica
        state = jax.device_get(jax.tree_map(lambda x: x[0], state))
        step = int(state.step)
        checkpoints.save_checkpoint(workdir, state, step, keep=3)


def _train_step(
    state: TrainState,
    batch: DataSetDict,
    learning_rate_fn: optax._src.base.Schedule,
    criterion: Callable,
) -> Tuple[TrainState, MetricsDict]:
    """Perform a single training step. Assummes sharded batched data.

    This function is intended to be used via :meth:`train_and_evaluate`, not directly.

    Args:
        state: Flax train state which includes the
           model apply function, the model parameters
           and an Optax optimizer.
        batch: Sharded and batched training data.
        learning_rate_fn: A function to map step
           counts to values.
        criterion: A function that specifies the loss being minimized in training. Default: :meth:`mse_loss`.

    Returns:
        Updated parameters and diagnostic statistics.
    """

    def loss_fn(params: PyTree):
        """Loss function used for training."""
        output, new_model_state = state.apply_fn(
            {
                "params": params,
                "batch_stats": state.batch_stats,
            },
            batch["image"],
            mutable=["batch_stats"],
        )
        loss = criterion(output, batch["label"])
        return loss, (new_model_state, output)

    step = state.step
    lr = learning_rate_fn(step)

    grad_fn = jax.value_and_grad(loss_fn, has_aux=True)
    aux, grads = grad_fn(state.params)
    # Re-use same axis_name as in call to pmap
    grads = lax.pmean(grads, axis_name="batch")
    new_model_state, output = aux[1]
    metrics = compute_metrics(output, batch["label"])
    metrics["learning_rate"] = lr

    # Update params and stats
    new_state = state.apply_gradients(
        grads=grads,
        batch_stats=new_model_state["batch_stats"],
    )

    return new_state, metrics


def construct_traversal(prmname: str) -> ModelParamTraversal:
    """Construct utility to select model parameters using a name filter.

    Args:
        prmname: Name of parameter to select.

    Returns:
        Flax utility to traverse and select model parameters.
    """
    return ModelParamTraversal(lambda path, _: prmname in path)


def clip_positive(params: PyTree, traversal: ModelParamTraversal, minval: float = 1e-4) -> PyTree:
    """Clip parameters to positive range.

    Args:
        params: Current model parameters.
        traversal: Utility to select model parameters.
        minval: Minimum value to clip selected model parameters
            and keep them in a positive range. Default: 1e-4.
    """
    params_out = traversal.update(lambda x: jnp.clip(x, a_min=minval), unfreeze(params))

    return freeze(params_out)


def clip_range(
    params: PyTree, traversal: ModelParamTraversal, minval: float = 1e-4, maxval: float = 1
) -> PyTree:
    """Clip parameters to specified range.

    Args:
        params: Current model parameters.
        traversal: Utility to select model parameters.
        minval: Minimum value to clip selected model parameters. Default: 1e-4.
        maxval: Maximum value to clip selected model parameters. Default: 1.
    """
    params_out = traversal.update(
        lambda x: jnp.clip(x, a_min=minval, a_max=maxval), unfreeze(params)
    )

    return freeze(params_out)


def _train_step_post(
    state: TrainState,
    batch: DataSetDict,
    learning_rate_fn: optax._src.base.Schedule,
    criterion: Callable,
    train_step_fn: Callable,
    post_lst: List[Callable],
) -> Tuple[TrainState, MetricsDict]:
    """Perform a single training step. A list of postprocessing
    functions (i.e. for spectral normalization or positivity
    condition, etc.) is applied after the gradient update.
    Assumes sharded batched data.

    This function is intended to be used via :meth:`train_and_evaluate`, not directly.

    Args:
        state: Flax train state which includes the
           model apply function, the model parameters
           and an Optax optimizer.
        batch: Sharded and batched training data.
        learning_rate_fn: A function to map step
           counts to values.
        criterion: A function that specifies the loss being minimized in training.
        train_step_fn: A function that executes a training step.
        post_lst: List of postprocessing functions to apply to parameter set after optimizer step (e.g. clip
            to a specified range, normalize, etc.).

    Returns:
        Updated parameters, fulfilling additional constraints,
        and diagnostic statistics.
    """

    new_state, metrics = train_step_fn(state, batch, learning_rate_fn, criterion)

    # Post-process parameters
    for post_fn in post_lst:
        new_params = post_fn(new_state.params)
        new_state = new_state.replace(params=new_params)

    return new_state, metrics


def _eval_step(state: TrainState, batch: DataSetDict) -> MetricsDict:
    """Evaluate current model state. Assumes sharded
    batched data.

    This function is intended to be used via :meth:`train_and_evaluate` or :meth:`only_evaluate`, not directly.

    Args:
        state: Flax train state which includes the
           model apply function and the model parameters.
        batch: Sharded and batched training data.

    Returns:
        Current diagnostic statistics.
    """
    variables = {
        "params": state.params,
        "batch_stats": state.batch_stats,
    }
    output = state.apply_fn(variables, batch["image"], train=False, mutable=False)
    return compute_metrics(output, batch["label"])


# sync across replicas
def sync_batch_stats(state: TrainState) -> TrainState:
    """Sync the batch statistics across replicas."""
    # Each device has its own version of the running average batch
    # statistics and those are synced before evaluation
    return state.replace(batch_stats=cross_replica_mean(state.batch_stats))


# pmean only works inside pmap because it needs an axis name.
#: This function will average the inputs across all devices.
cross_replica_mean = jax.pmap(lambda x: lax.pmean(x, "x"), "x")


class ArgumentStruct:
    """Class that converts a python dictionary into an object with named entries given by the dictionary keys.

    After the object instantiation both modes of access (dictionary or object entries) can be used.
    """

    def __init__(self, **entries):
        self.__dict__.update(entries)


def stats_obj():
    """Functionality to log and store iteration statistics.

    This function initializes an object :class:`.diagnostics.IterationStats` to log and store
    iteration statistics if logging is enabled during training.
    The statistics collected are: epoch, time, learning rate, loss and snr in training and loss and snr in evaluation.
    The :class:`.diagnostics.IterationStats` object takes care of both: printing stats to command line and storing
    them for further analysis.
    """
    # epoch, time learning rate loss and snr (train and
    # eval) fields
    itstat_fields = {
        "Epoch": "%d",
        "Time": "%8.2e",
        "Train LR": "%.6f",
        "Train Loss": "%.6f",
        "Train SNR": "%.2f",
        "Eval Loss": "%.6f",
        "Eval SNR": "%.2f",
    }
    itstat_attrib = [
        "epoch",
        "time",
        "train_learning_rate",
        "train_loss",
        "train_snr",
        "loss",
        "snr",
    ]

    # dynamically create itstat_func; see https://stackoverflow.com/questions/24733831
    itstat_return = "return(" + ", ".join(["obj." + attr for attr in itstat_attrib]) + ")"
    scope: dict[str, Callable] = {}
    exec("def itstat_func(obj): " + itstat_return, scope)
    default_itstat_options: dict[str, Union[dict, Callable, bool]] = {
        "fields": itstat_fields,
        "itstat_func": scope["itstat_func"],
        "display": True,
    }
    itstat_insert_func: Callable = default_itstat_options.pop("itstat_func")  # type: ignore
    itstat_object = IterationStats(**default_itstat_options)  # type: ignore

    return itstat_object, itstat_insert_func


def train_and_evaluate(
    config: ConfigDict,
    workdir: str,
    model: ModuleDef,
    train_ds: DataSetDict,
    test_ds: DataSetDict,
    create_lr_schedule: Callable = create_cnst_lr_schedule,
    criterion: Callable = mse_loss,
    train_step_fn: Callable = _train_step,
    eval_step_fn: Callable = _eval_step,
    post_lst: Optional[List[Callable]] = None,
    variables0: Optional[ModelVarDict] = None,
    checkpointing: bool = False,
    log: bool = False,
) -> ModelVarDict:
    """Execute model training and evaluation loop.

    Args:
        config: Hyperparameter configuration.
        workdir: Directory to write checkpoints.
        model: Flax model to train.
        train_ds: Dictionary of training data (includes images
            and labels).
        test_ds: Dictionary of testing data (includes images
            and labels).
        create_lr_schedule: A function that creates an Optax
            learning rate schedule. Default:
            :meth:`create_cnst_schedule`.
        criterion: A function that specifies the loss being minimized in training. Default: :meth:`mse_loss`.
        train_step_fn: A hook for a function that executes a training step. Default: :meth:`_train_step`, i.e. use the standard train step.
        eval_step_fn: A hook for a function that executes an eval step. Default: :meth:`_eval_step`, i.e. use the standard eval step.
        post_lst: List of postprocessing functions to apply to parameter set after optimizer step (e.g. clip
            to a specified range, normalize, etc.).
        variables0: Optional initial state of model
            parameters. Default: ``None``.
        checkpointing: A flag for checkpointing model state.
            Default: ``False``. `RunTimeError` is generated if
            ``True`` and tensorflow is not available.
        log: A flag for logging to the interface the evolution of results. Default: ``False``.

    Returns:
        Model variables extracted from TrainState.
    """
    itstat_object = None
    if log:  # pragma: no cover
        print(
            "Channels: %d, training signals: %d, testing"
            " signals: %d, signal size: %d"
            % (
                train_ds["label"].shape[-1],
                train_ds["label"].shape[0],
                test_ds["label"].shape[0],
                train_ds["label"].shape[1],
            )
        )

    # Configure seed.
    key = jax.random.PRNGKey(config["seed"])
    # Split seed for data iterators and model initialization
    key1, key2 = jax.random.split(key)

    # Determine sharded vs. batch partition
    if config["batch_size"] % jax.device_count() > 0:
        raise ValueError("Batch size must be divisible by the number of devices")
    local_batch_size = config["batch_size"] // jax.process_count()
    size_device_prefetch = 2  # Set for GPU

    # Determine monitoring steps
    steps_per_epoch = train_ds["image"].shape[0] // config["batch_size"]
    config["steps_per_epoch"] = steps_per_epoch  # needed for creating lr schedule
    if config["num_train_steps"] == -1:
        num_steps = int(steps_per_epoch * config["num_epochs"])
    else:
        num_steps = config["num_train_steps"]
    num_validation_examples = test_ds["image"].shape[0]
    if config["steps_per_eval"] == -1:
        steps_per_eval = num_validation_examples // config["batch_size"]
    else:
        steps_per_eval = config["steps_per_eval"]
    steps_per_checkpoint = steps_per_epoch * 10

    # Construct data iterators
    train_dt_iter = create_input_iter(
        key1,
        train_ds,
        local_batch_size,
        size_device_prefetch,
        model.dtype,
        train=True,
    )
    eval_dt_iter = create_input_iter(
        key1,  # eval: no permutation
        test_ds,
        local_batch_size,
        size_device_prefetch,
        model.dtype,
        train=False,
    )

    # Create Flax training state
    ishape = train_ds["image"].shape[1:3]
    lr_schedule = create_lr_schedule(config)
    state = create_train_state(key2, config, model, ishape, lr_schedule, variables0)
    if checkpointing and variables0 is None:
        # Only restore if no initialization is provided
        if have_tf:  # Flax checkpointing requires tensorflow
            state = restore_checkpoint(state, workdir)
        else:
            raise RuntimeError(
                "Tensorflow not available and it is required for Flax checkpointing."
            )
    if log:  # pragma: no cover
        print(get_parameter_overview(state.params))
        print(get_parameter_overview(state.batch_stats))
    step_offset = int(state.step)  # > 0 if restarting from checkpoint

    # For parallel training
    state = jax_utils.replicate(state)
    if post_lst is not None:
        p_train_step = jax.pmap(
            functools.partial(
                _train_step_post,
                train_step_fn=train_step_fn,
                learning_rate_fn=lr_schedule,
                criterion=criterion,
                post_lst=post_lst,
            ),
            axis_name="batch",
        )
    else:
        p_train_step = jax.pmap(
            functools.partial(train_step_fn, learning_rate_fn=lr_schedule, criterion=criterion),
            axis_name="batch",
        )
    p_eval_step = jax.pmap(eval_step_fn, axis_name="batch")

    # Execute training loop and register stats
    train_metrics: List[Any] = []
    eval_metrics: List[Any] = []
    t0 = time.time()
    if log:
        print("Initial compilation, this might take some minutes...")
        itstat_object, itstat_insert_func = stats_obj()

    for step, batch in zip(range(step_offset, num_steps), train_dt_iter):
        state, metrics = p_train_step(state, batch)
        if log and step == step_offset:
            print("Initial compilation completed.")

        if log:  # pragma: no cover
            train_metrics.append(metrics)
            if (step + 1) % config["log_every_steps"] == 0:
                train_metrics = common_utils.get_metrics(train_metrics)
                summary = {
                    f"train_{k}": v
                    for k, v in jax.tree_map(lambda x: x.mean(), train_metrics).items()
                }

                epoch = step // steps_per_epoch
                summary["epoch"] = epoch
                summary["time"] = time.time() - t0
                train_metrics = []

                # sync batch statistics across replicas
                state = sync_batch_stats(state)
                for _ in range(steps_per_eval):
                    eval_batch = next(eval_dt_iter)
                    metrics = p_eval_step(state, eval_batch)
                    eval_metrics.append(metrics)
                eval_metrics = common_utils.get_metrics(eval_metrics)

                summary_eval = jax.tree_map(lambda x: x.mean(), eval_metrics)
                summary.update(summary_eval)
                eval_metrics = []

                itstat_object.insert(itstat_insert_func(ArgumentStruct(**summary)))

        if (step + 1) % steps_per_checkpoint == 0 or step + 1 == num_steps:
            state = sync_batch_stats(state)
            if checkpointing:  # pragma: no cover
                if not have_tf:  # Flax checkpointing requires tensorflow
                    raise RuntimeError(
                        "Tensorflow not available and it is" " required for Flax checkpointing."
                    )
                save_checkpoint(state, workdir)

    jax.random.normal(jax.random.PRNGKey(0), ()).block_until_ready()
    if log:
        itstat_object.end()

    state = sync_batch_stats(state)
    # Extract one copy of state
    state = jax_utils.unreplicate(state)
    dvar: ModelVarDict = {
        "params": state.params,
        "batch_stats": state.batch_stats,
    }

    return dvar, itstat_object


def _apply_fn(model: ModuleDef, variables: ModelVarDict, batch: DataSetDict) -> Array:
    """Apply current model. Assumes sharded
    batched data and replicated variables for distributed processing.

    This function is intended to be used via :meth:`only_apply`, not directly.

    Args:
        model: Flax model to apply.
        variables: State of model parameters (replicated).
        batch: Sharded and batched training data.

    Returns:
        Output computed by given model.
    """
    output = model.apply(variables, batch["image"], train=False, mutable=False)
    return output


def only_apply(
    config: ConfigDict,
    workdir: str,
    model: ModuleDef,
    test_ds: DataSetDict,
    apply_fn: Callable = _apply_fn,
    variables: Optional[ModelVarDict] = None,
    checkpointing: bool = False,
) -> Tuple[Array, ModelVarDict]:
    """Execute model application loop.

    Args:
        config: Hyperparameter configuration.
        workdir: Directory to read checkpoint (if enabled).
        model: Flax model to apply.
        test_ds: Dictionary of testing data (includes images
            and labels).
        apply_fn: A hook for a function that applies current model. Default: :meth:`_apply_fn`, i.e. use the standard apply function.
        variables: Model parameters to use for evaluation.
            Default: ``None`` (i.e. read from checkpoint).
        checkpointing: A flag for checkpointing model state.
            Default: ``False``. `RunTimeError` is generated if
            ``True`` and tensorflow is not available.

    Returns:
        Output of model evaluated at the input provided in `test_ds`.

    Raises:
        Error if no state and no checkpoint are specified.
    """
    if variables is None:
        if checkpointing:
            if not have_tf:
                raise RuntimeError(
                    "Tensorflow not available and it is " "required for Flax checkpointing."
                )
            state = checkpoints.restore_checkpoint(workdir, model)
            variables = {
                "params": state["params"],
                "batch_stats": state["batch_stats"],
            }
            print(get_parameter_overview(variables["params"]))
            print(get_parameter_overview(variables["batch_stats"]))
        else:
            raise Exception("No variables or checkpoint provided")

    # For distributed testing
    local_batch_size = config["batch_size"] // jax.process_count()
    size_device_prefetch = 2  # Set for GPU
    # Configure seed.
    key = jax.random.PRNGKey(config["seed"])
    # Set data iterator
    eval_dt_iter = create_input_iter(
        key,  # eval: no permutation
        test_ds,
        local_batch_size,
        size_device_prefetch,
        model.dtype,
        train=False,
    )
    p_apply_step = jax.pmap(apply_fn, axis_name="batch", static_broadcasted_argnums=0)

    # Evaluate model with provided variables
    variables = jax_utils.replicate(variables)
    num_examples = test_ds["image"].shape[0]
    steps_ = num_examples // config["batch_size"]
    output = []
    for _ in range(steps_):
        eval_batch = next(eval_dt_iter)
        output_ = p_apply_step(model, variables, eval_batch)
        output.append(output_.reshape((-1,) + output_.shape[-3:]))

    # Allow for completing the async run
    jax.random.normal(jax.random.PRNGKey(0), ()).block_until_ready()

    # Extract one copy of variables
    variables = jax_utils.unreplicate(variables)
    # Convert to array
    output = jnp.array(output)
    # Remove leading dimension
    output = output.reshape((-1,) + output.shape[-3:])

    return output, variables
