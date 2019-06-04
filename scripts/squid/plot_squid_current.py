# -----------------------------------------------------------------------------
# Copyright 2019 by ShabaniPy Authors, see AUTHORS for more details.
#
# Distributed under the terms of the MIT license.
#
# The full license is in the file LICENCE, distributed with this software.
# -----------------------------------------------------------------------------
"""Plot the squid current for different set of parameters.

"""

#: Parameters of the first junction: amplitude, transparency
FIRST_JUNCTION = (1, 0.)

#: Transparencies of the second junction
SECOND_JUNCTION_TRANSPARENCIES = [0.0, 0.6, 0.9]

#: Amplitudes of the second junction
SECOND_JUNCTION_AMPLITUDES = [0.1, 0.25, 0.5, 1]

# =============================================================================
# --- Execution ---------------------------------------------------------------
# =============================================================================
import numpy as np
import matplotlib.pyplot as plt

from shabanipy.squid.squid_model import compute_squid_current
from shabanipy.squid.cpr import finite_transparency_jj_current as cpr

phase_diff = np.linspace(-2*np.pi, 2*np.pi, 1001)

for t in SECOND_JUNCTION_TRANSPARENCIES:
    plt.figure()
    for a in SECOND_JUNCTION_AMPLITUDES:
        squid = compute_squid_current(phase_diff,
                                      cpr, (0, *FIRST_JUNCTION),
                                      cpr, (0, a, t))
        amplitude = (np.amax(squid) - np.min(squid))
        baseline = (np.amax(squid) + np.min(squid))/2
        plt.plot(phase_diff,
                 (squid - baseline)/amplitude,
                 label=f't={t}, a={a}')
    plt.legend()
plt.show()