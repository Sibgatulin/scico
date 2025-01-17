#!/usr/bin/env python
# -*- coding: utf-8 -*-
# This file is part of the SCICO package. Details of the copyright
# and user license can be found in the 'LICENSE.txt' file distributed
# with the package.

r"""
TV-Regularized Abel Inversion Tuning Demo
=========================================

This example demonstrates the use of
[scico.ray.tune](../_autosummary/scico.ray.tune.rst) to tune
parameters for the companion [example script](ct_abel_tv_admm.rst).

This script is hard-coded to run on CPU only to avoid the large number of
warnings that are emitted when GPU resources are requested but not available,
and due to the difficulty of supressing these warnings in a way that does
not force use of the CPU only. To enable GPU usage, comment out the
`os.environ` statements near the beginning of the script, and change the
value of the "gpu" entry in the `resources` dict from 0 to 1. Note that
two environment variables are set to suppress the warnings because
`JAX_PLATFORMS` was intended to replace `JAX_PLATFORM_NAME` but this change
has yet to be correctly implemented
(see [google/jax#6805](https://github.com/google/jax/issues/6805) and
[google/jax#10272](https://github.com/google/jax/pull/10272).
"""

# isort: off
import os

os.environ["JAX_PLATFORM_NAME"] = "cpu"
os.environ["JAX_PLATFORMS"] = "cpu"

import numpy as np

import jax

import scico.ray as ray
from scico import functional, linop, loss, metric, plot
from scico.examples import create_circular_phantom
from scico.linop.abel import AbelProjector
from scico.optimize.admm import ADMM, LinearSubproblemSolver
from scico.ray import tune

"""
Create a ground truth image.
"""
N = 256  # phantom size
x_gt = create_circular_phantom((N, N), [0.4 * N, 0.2 * N, 0.1 * N], [1, 0, 0.5])


"""
Set up the forward operator and create a test measurement.
"""
A = AbelProjector(x_gt.shape)
y = A @ x_gt
np.random.seed(12345)
y = y + np.random.normal(size=y.shape)
ATy = A.T @ y

"""
Put main arrays into ray object store.
"""
ray_x_gt, ray_y = ray.put(np.array(x_gt)), ray.put(np.array(y))


"""
Define performance evaluation function.
"""


def eval_params(config, reporter):
    # Extract solver parameters from config dict.
    λ, ρ = config["lambda"], config["rho"]
    # Get main arrays from ray object store.
    x_gt, y = ray.get([ray_x_gt, ray_y])
    # Put main arrays on jax device.
    x_gt, y = jax.device_put([x_gt, y])
    # Set up problem to be solved.
    A = AbelProjector(x_gt.shape)
    f = loss.SquaredL2Loss(y=y, A=A)
    g = λ * functional.L1Norm()
    C = linop.FiniteDifference(input_shape=x_gt.shape)
    # Define solver.
    solver = ADMM(
        f=f,
        g_list=[g],
        C_list=[C],
        rho_list=[ρ],
        x0=A.inverse(y),
        maxiter=10,
        subproblem_solver=LinearSubproblemSolver(),
    )
    # Perform 100 iterations, reporting performance to ray.tune every 10 iterations.
    for step in range(10):
        x_admm = solver.solve()
        reporter(psnr=float(metric.psnr(x_gt, x_admm)))


"""
Define parameter search space and resources per trial.
"""
config = {"lambda": tune.loguniform(1e0, 1e2), "rho": tune.loguniform(1e1, 1e3)}
resources = {"gpu": 0, "cpu": 1}  # gpus per trial, cpus per trial


"""
Run parameter search.
"""
analysis = tune.run(
    eval_params,
    metric="psnr",
    mode="max",
    num_samples=100,
    config=config,
    resources_per_trial=resources,
    hyperopt=True,
    verbose=True,
)

"""
Display best parameters and corresponding performance.
"""
best_config = analysis.get_best_config(metric="psnr", mode="max")
print(f"Best PSNR: {analysis.get_best_trial().last_result['psnr']:.2f} dB")
print("Best config: " + ", ".join([f"{k}: {v:.2e}" for k, v in best_config.items()]))


"""
Plot parameter values visited during parameter search. Marker sizes are
proportional to number of iterations run at each parameter pair. The best
point in the parameter space is indicated in red.
"""
fig = plot.figure(figsize=(8, 8))
for t in analysis.trials:
    n = t.metric_analysis["training_iteration"]["max"]
    plot.plot(
        t.config["lambda"],
        t.config["rho"],
        ptyp="loglog",
        lw=0,
        ms=(0.5 + 1.5 * n),
        marker="o",
        mfc="blue",
        mec="blue",
        fig=fig,
    )
plot.plot(
    best_config["lambda"],
    best_config["rho"],
    ptyp="loglog",
    title="Parameter search sampling locations\n(marker size proportional to number of iterations)",
    xlbl=r"$\rho$",
    ylbl=r"$\lambda$",
    lw=0,
    ms=5.0,
    marker="o",
    mfc="red",
    mec="red",
    fig=fig,
)
ax = fig.axes[0]
ax.set_xlim([config["rho"].lower, config["rho"].upper])
ax.set_ylim([config["lambda"].lower, config["lambda"].upper])
fig.show()


"""
Plot parameter values visited during parameter search and corresponding
reconstruction PSNRs.The best point in the parameter space is indicated
in red.
"""
𝜌 = [t.config["rho"] for t in analysis.trials]
𝜆 = [t.config["lambda"] for t in analysis.trials]
psnr = [t.metric_analysis["psnr"]["max"] for t in analysis.trials]
minpsnr = min(max(psnr), 20.0)
𝜌, 𝜆, psnr = zip(*filter(lambda x: x[2] >= minpsnr, zip(𝜌, 𝜆, psnr)))
fig, ax = plot.subplots(figsize=(10, 8))
sc = ax.scatter(𝜌, 𝜆, c=psnr, cmap=plot.cm.plasma_r)
fig.colorbar(sc)
plot.plot(
    best_config["lambda"],
    best_config["rho"],
    ptyp="loglog",
    lw=0,
    ms=12.0,
    marker="2",
    mfc="red",
    mec="red",
    fig=fig,
    ax=ax,
)
ax.set_xscale("log")
ax.set_yscale("log")
ax.set_xlabel(r"$\rho$")
ax.set_ylabel(r"$\lambda$")
ax.set_title("PSNR at each sample location\n(values below 20 dB omitted)")
fig.show()


input("\nWaiting for input to close figures and exit")
