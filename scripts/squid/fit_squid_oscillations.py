# -----------------------------------------------------------------------------
# Copyright 2021 by ShabaniPy Authors, see AUTHORS for more details.
#
# Distributed under the terms of the MIT license.
#
# The full license is in the file LICENCE, distributed with this software.
# -----------------------------------------------------------------------------
"""Fit SQUID oscillations to a two-junction CPR model."""
import argparse
import warnings
from configparser import ConfigParser, ExtendedInterpolation
from contextlib import redirect_stdout
from functools import partial
from importlib import import_module
from pathlib import Path

import corner
import numpy as np
from lmfit import Model
from lmfit.model import save_modelresult
from matplotlib import pyplot as plt
from pandas import DataFrame
from scipy.constants import eV

from shabanipy.dvdi import extract_switching_current
from shabanipy.labber import LabberData, get_data_dir
from shabanipy.plotting import jy_pink, plot, plot2d
from shabanipy.squid import estimate_boffset, estimate_frequency
from shabanipy.utils import to_dataframe

from squid_model_func import squid_model_func

print = partial(print, flush=True)

# set up the command-line interface
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "config_path", help="path to .ini config file, relative to this script."
)
parser.add_argument("config_section", help="section of the .ini config file to use")
parser.add_argument(
    "--both-branches",
    "-b",
    default=False,
    action="store_true",
    help="fit +ve and -ve critical current branches simultaneously",
)
parser.add_argument(
    "--dry-run",
    "-n",
    default=False,
    action="store_true",
    help="do preliminary analysis but don't run fit",
)
parser.add_argument(
    "--plot-guess",
    "-g",
    default=False,
    action="store_true",
    help="plot the initial guess along with the best fit",
)
parser.add_argument(
    "--conf-interval",
    "-c",
    nargs="*",
    type=int,
    metavar=("σ1", "σ2"),
    help="calculate confidence intervals (optional list of ints specifying sigma values to pass to lmfit.conf_interval)",
)
parser.add_argument(
    "--emcee",
    "-m",
    default=False,
    action="store_true",
    help="run a Markov Chain Monte Carlo sampler and plot with `corner`",
)
parser.add_argument(
    "--verbose",
    "-v",
    default=False,
    action="store_true",
    help="print more information to stdout",
)
args = parser.parse_args()

# load the config file
with open(Path(__file__).parent / args.config_path) as f:
    print(f"Using config file `{f.name}`")
    ini = ConfigParser(interpolation=ExtendedInterpolation())
    ini.read_file(f)
    config = ini[args.config_section]

# get the path to the datafile
INPATH = Path(config.get("LABBERDATA_DIR", get_data_dir())) / config["DATAPATH"]

# magnet coil current-per-field conversion factor
AMPS_PER_T = getattr(
    import_module("shabanipy.constants"),
    f"{config['FRIDGE'].upper()}_AMPS_PER_TESLA_{config['PERP_AXIS'].upper()}",
)
# sanity check conversion factor is correct (relies on my local file hierarchy)
if config["FRIDGE"] not in str(INPATH):
    warnings.warn(
        f"I can't double check that {config['DATAPATH']} is from {config['FRIDGE']}"
    )

# set up plot styles
jy_pink.register()
plt.style.use(["jy_pink", "fullscreen13"])

# set up output directory and filename prefix
OUTDIR = (
    f"{__file__.split('.py')[0].replace('_', '-')}-results/"
    f"{config['WAFER']}-{config['PIECE']}_{config['LAYOUT']}/"
    f"{config['DEVICE']}"
)
print(f"All output will be saved to `{OUTDIR}`")
Path(OUTDIR).mkdir(parents=True, exist_ok=True)
OUTPATH = Path(OUTDIR) / f"{config['COOLDOWN']}-{config['SCAN']}"

# load the data
with LabberData(INPATH) as f:
    bfield = f.get_data(config["CH_FIELD_PERP"]) / AMPS_PER_T
    ibias, lockin = f.get_data(config["CH_LOCKIN"], get_x=True)
    dvdi = np.abs(lockin)
    temp_meas = f.get_data(config["CH_TEMP_MEAS"])
    f.warn_not_constant(config["CH_TEMP_MEAS"])

# plot the raw data
fig, ax = plot2d(
    *np.broadcast_arrays(bfield[..., np.newaxis] / 1e-3, ibias / 1e-6, dvdi),
    xlabel="x coil field (mT)",
    ylabel="dc bias (μA)",
    zlabel="dV/dI (Ω)",
    title="raw data",
    stamp=config["COOLDOWN"] + "_" + config["SCAN"],
)
fig.savefig(str(OUTPATH) + "_raw-data.png")

# extract the switching currents
ic_n, ic_p = extract_switching_current(
    ibias,
    dvdi,
    side="both",
    threshold=config.getfloat("RESISTANCE_THRESHOLD", fallback=None),
    interp=True,
)
ax.set_title("$I_c$ extraction")
plot(bfield / 1e-3, ic_p / 1e-6, ax=ax, color="k", lw=1)
plot(bfield / 1e-3, ic_n / 1e-6, ax=ax, color="k", lw=1)
fig.savefig(str(OUTPATH) + "_ic-extraction.png")

# in vector10, positive Bx points into the daughterboard (depends on mount orientation)
if config["FRIDGE"] == "vector10":
    bfield = np.flip(bfield) * -1
    ic_p = np.flip(ic_p)
    ic_n = np.flip(ic_n)
# in vector9, positive Bx points out of the daughterboard
elif config["FRIDGE"] == "vector9":
    pass
else:
    warnings.warn(f"I don't recognize fridge `{config['FRIDGE']}`")


model = Model(squid_model_func, both_branches=args.both_branches)

# initialize the parameters
params = model.make_params()
params["transparency1"].set(value=0.5, max=1)
params["transparency2"].set(value=0.5, max=1)
if config.getboolean("EQUAL_TRANSPARENCIES"):
    params["transparency2"].set(expr="transparency1")
params["switching_current1"].set(value=(np.max(ic_p) - np.min(ic_p)) / 2)
params["switching_current2"].set(value=np.mean(ic_p))
boffset, peak_idxs = estimate_boffset(
    bfield, ic_p, ic_n if args.both_branches else None
)
params["bfield_offset"].set(value=boffset)
cyc_per_T, (freqs, abs_fft) = estimate_frequency(bfield, ic_p)
params["radians_per_tesla"].set(value=2 * np.pi * cyc_per_T)
# anomalous phases; if both fixed, then there is no phase freedom in the model (aside
# from bfield_offset), as the two gauge-invariant phases are fixed by two constraints:
#     1. flux quantization:         γ1 - γ2 = 2πΦ/Φ_0 (mod 2π),
#     2. supercurrent maximization: I_tot = max_γ1 { I_1(γ1) + I_2(γ1 - 2πΦ/Φ_0) }
params["anom_phase1"].set(value=0, vary=False)
params["anom_phase2"].set(value=0, vary=False)
params["temperature"].set(value=round(np.mean(temp_meas), 3), vary=False)
params["gap"].set(value=200e-6 * eV, vary=False)
params["inductance"].set(value=1e-9)

# plot the radians_per_tesla estimate
fig, ax = plot(
    freqs / 1e3,
    abs_fft,
    xlabel="frequency [mT$^{-1}$]",
    ylabel="|FFT| [arb.]",
    title="frequency estimate",
    stamp=config["COOLDOWN"] + "_" + config["SCAN"],
    marker="o",
)
ax.set_ylim(0, np.max(abs_fft[1:]) * 1.05)  # ignore dc component
ax.axvline(cyc_per_T / 1e3, color="k")
ax.text(
    0.3,
    0.5,
    f"frequency $\sim$ {np.round(cyc_per_T / 1e3)} mT$^{{-1}}$\n"
    f"period $\sim$ {round(1 / cyc_per_T / 1e-6)} μT",
    transform=ax.transAxes,
)
fig.savefig(str(OUTPATH) + "_fft.png")

# plot the bfield_offset estimate
if args.both_branches:
    fig, (ax, ax2) = plt.subplots(2, 1, sharex=True)
    ax2.set_xlabel("x coil field [mT]")
    plot(
        bfield / 1e-3, ic_n / 1e-6, ylabel="supercurrent [μA]", ax=ax2, marker=".",
    )
    ax2.plot(
        bfield[peak_idxs[-1]] / 1e-3, ic_n[peak_idxs[-1]] / 1e-6, lw=0, marker="o",
    )
    ax2.axvline(boffset / 1e-3, color="k")
else:
    fig, ax = plt.subplots()
    ax.set_xlabel("x coil field [mT]")
plot(
    bfield / 1e-3,
    ic_p / 1e-6,
    ylabel="supercurrent [μA]",
    title="bfield offset estimate",
    stamp=config["COOLDOWN"] + "_" + config["SCAN"],
    ax=ax,
    marker=".",
)
ax.axvline(boffset / 1e-3, color="k")
ax.plot(
    bfield[peak_idxs[0]] / 1e-3, ic_p[peak_idxs[0]] / 1e-6, lw=0, marker="o",
)
ax.text(
    0.5,
    0.5,
    f"bfield offset $\\approx$ {np.round(boffset / 1e-3, 3)} mT",
    va="center",
    ha="center",
    transform=ax.transAxes,
)
fig.savefig(str(OUTPATH) + "_bfield-offset.png")

# scale the residuals to get a somewhat meaningful χ2 value
ibias_step = np.diff(ibias, axis=-1)
uncertainty = np.mean(ibias_step)
if not np.allclose(ibias_step, uncertainty):
    warnings.warn(
        "Bias current has variable step sizes; "
        "the magnitude of the χ2 statistic may not be meaningful."
    )

# fit the data
if not args.dry_run:
    print("Optimizing fit...", end="")
    result = model.fit(
        data=np.array([ic_p, ic_n]).flatten() if args.both_branches else ic_p,
        weights=1 / uncertainty,
        bfield=bfield,
        params=params,
        verbose=args.verbose,
    )
    print("...done.")
    print(result.fit_report())
    with open(str(OUTPATH) + "_fit-report.txt", "w") as f:
        f.write(result.fit_report())
    with open(str(OUTPATH) + "_fit-params.txt", "w") as f, redirect_stdout(f):
        print(to_dataframe(result.params))

    if args.conf_interval is not None:
        print("Calculating confidence intervals (this takes a while)...", end="")
        ci_kwargs = dict(verbose=args.verbose)
        if args.conf_interval:
            ci_kwargs.update(dict(sigmas=args.conf_interval))
        result.conf_interval(**ci_kwargs)
        print("...done.")
        print(result.ci_report(ndigits=10))

    save_modelresult(result, str(OUTPATH) + "_model-result.json")

    if args.emcee:
        print("Calculating posteriors with emcee...")
        mcmc_result = model.fit(
            data=np.array([ic_p, ic_n]).flatten() if args.both_branches else ic_p,
            weights=1 / uncertainty,
            bfield=bfield,
            params=result.params,
            # nan_policy="omit",
            method="emcee",
            fit_kws=dict(steps=1000, nwalkers=100, burn=200, thin=10, is_weighted=True),
        )
        print("...done.")
        save_modelresult(mcmc_result, str(OUTPATH) + "_mcmc-result.json")
        print("\nemcee medians and (averaged) +-1σ quantiles")
        print("------------------------------")
        print(mcmc_result.fit_report())
        print("\nemcee max likelihood estimates")
        print("------------------------------")
        mle_loc = np.argmax(mcmc_result.lnprob)
        mle_loc = np.unravel_index(mle_loc, mcmc_result.lnprob.shape)
        mle = mcmc_result.chain[mle_loc]
        for i, p in enumerate([p for p in mcmc_result.params.values() if p.vary]):
            print(f"{p.name}: {mle[i]}")

        fig, ax = plt.subplots()
        ax.plot(mcmc_result.acceptance_fraction)
        ax.set_xlabel("walker")
        ax.set_ylabel("acceptance fraction")
        fig.savefig(str(OUTPATH) + "_emcee-acceptance-fraction.png")
        with plt.style.context("classic"):
            fig = corner.corner(
                mcmc_result.flatchain,
                labels=mcmc_result.var_names,
                truths=[p.value for p in mcmc_result.params.values() if p.vary],
                labelpad=0.1,
            )
            fig.savefig(str(OUTPATH) + "_emcee-corner.png")


# plot the best fit and initial guess over the data
popt = result.params.valuesdict() if not args.dry_run else params
phase = (bfield - popt["bfield_offset"]) * popt["radians_per_tesla"]
if args.both_branches:
    fig, (ax_p, ax_n) = plt.subplots(2, 1, sharex=True)
    plot(
        phase / (2 * np.pi),
        ic_n / 1e-6,
        ax=ax_n,
        xlabel="phase [2π]",
        ylabel="switching current [μA]",
        label="data",
        marker=".",
    )
    if not args.dry_run:
        best_p, best_n = np.split(result.best_fit, 2)
        plot(phase / (2 * np.pi), best_n / 1e-6, ax=ax_n, label="fit")
    init_p, init_n = np.split(model.eval(bfield=bfield, params=params), 2)
    if args.plot_guess:
        plot(phase / (2 * np.pi), init_n / 1e-6, ax=ax_n, label="guess")
else:
    fig, ax_p = plt.subplots()
    if not args.dry_run:
        best_p = result.best_fit
    init_p = model.eval(bfield=bfield, params=params)
plot(
    phase / (2 * np.pi),
    ic_p / 1e-6,
    ax=ax_p,
    xlabel="phase [2π]",
    ylabel="switching current [μA]",
    label="data",
    marker=".",
    stamp=config["COOLDOWN"] + "_" + config["SCAN"],
)
if not args.dry_run:
    plot(phase / (2 * np.pi), best_p / 1e-6, ax=ax_p, label="fit")
if args.plot_guess:
    plot(phase / (2 * np.pi), init_p / 1e-6, ax=ax_p, label="guess")
fig.savefig(str(OUTPATH) + "_fit.png")
if not args.dry_run:
    DataFrame(
        {
            "bfield": bfield,
            "phase": phase,
            "ic_p": ic_p,
            "fit_p": best_p,
            "init_p": init_p,
            **(
                {"ic_n": ic_n, "fit_n": best_n, "init_n": init_n}
                if args.both_branches
                else {}
            ),
        }
    ).to_csv(str(OUTPATH) + "_fit.csv", index=False)
plt.show()