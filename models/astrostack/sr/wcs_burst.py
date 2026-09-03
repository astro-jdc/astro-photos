"""Tier B: burst super-resolution with WCS warping instead of learned alignment.

The adaptation the research note prescribes (section 1, "critical caveat for
transfer"; section 9, Tier B step 2):

    Start from RBSR (variable N, recurrent) or Burstormer/BSRT (strong fusion)
    but **delete the alignment stage and substitute WCS-driven warping**; you
    already know the geometry to sub-pixel accuracy, which is a decisive
    advantage over consumer burst SR.

So the network here has **no** optical-flow, no deformable-convolution
alignment, and no ShiftNet. Registration is done analytically by
:func:`wcs_warp_stack` — plain :mod:`reproject`, the same code path Tier A
uses — and the network is left to do only what it is actually good at: fusion,
deconvolution and denoising.

Three structural choices, each traceable to the note:

**Recurrent over the burst (RBSR).** Frames are folded in one at a time
against a persistent hidden state seeded by the base frame. That makes the
model *flexible in N*: a target with 3 contributions and a target with 5,000
go through the same weights. A fixed-input CNN cannot do that, and N per
target in this repository varies by three orders of magnitude.

**Explicit physical conditioning.** Every frame carries its own PSF kernel,
sigma map, zero point, background and airmass into the fusion cell as side
inputs. Unrolled / physics-informed formulations generalise across varying
measurement configurations where black-box fusion does not — which is exactly
the variable-N, variable-geometry regime here.

**A heteroscedastic head.** The network predicts ``log sigma**2`` alongside
the image, so the mandatory uncertainty map is a first-class output rather
than an afterthought. See :mod:`astrostack.sr.uncertainty`.

The warping half of this module is implemented and runs today with no torch.
The network half is scaffolding: the architecture, the conditioning, the
forward pass and the weight I/O are all written out, but it ships **untrained**
and :meth:`WCSBurstSR.enhance` refuses to run without weights rather than
returning a plausible-looking image from random parameters.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from astrostack.align.register import OutputGrid, reproject_frame
from astrostack.errors import AstroStackError
from astrostack.io.frame import Frame
from astrostack.logging import get_logger
from astrostack.sr._torch import select_device, torch
from astrostack.sr.base import SRInputs, SRResult, SuperResolver, register_resolver

__all__ = ["WCSBurstSR", "build_condition_channels", "wcs_warp_stack"]

log = get_logger(__name__)


def wcs_warp_stack(
    inputs: SRInputs,
    grid: OutputGrid,
    method: str = "adaptive",
) -> tuple[np.ndarray, np.ndarray, list[Frame]]:
    """Analytic replacement for the learned alignment module.

    Returns ``(data_stack, valid_stack, warped_frames)`` with shapes
    ``(N, H, W)``. Sub-pixel accuracy comes from the plate solution, which is
    better than any optical flow a network could learn from this data — and,
    unlike a learned warp, it cannot invent a displacement field that moves
    real structure.
    """
    warped = [reproject_frame(fr, grid, method=method) for fr in inputs.frames]
    data = np.stack([w.data for w in warped]).astype(np.float32)
    valid = np.stack([w.good for w in warped]).astype(np.float32)
    return data, valid, warped


def build_condition_channels(inputs: SRInputs, shape: tuple[int, int]) -> np.ndarray:
    """Per-frame physical side inputs, broadcast to ``(N, C, H, W)``.

    Channels, in order:

    0. sigma map (per pixel) — the only spatially varying one;
    1. PSF FWHM in output pixels;
    2. PSF ellipticity;
    3. airmass;
    4. zero point;
    5. background level;
    6. ``photometrically_unreliable`` flag (1.0 for a JPEG contribution).

    Scalars are broadcast rather than injected through FiLM/embeddings so that
    the scaffolding stays readable; a trained implementation would likely
    replace channels 1-6 with a small conditioning MLP feeding FiLM
    parameters, which is a strict generalisation of what is written here.
    """
    n = len(inputs.frames)
    h, w = shape
    out = np.zeros((n, 7, h, w), dtype=np.float32)
    for i, fr in enumerate(inputs.frames):
        sigma = inputs.sigma_maps[i] if i < len(inputs.sigma_maps) else None
        if sigma is not None and sigma.shape == shape:
            out[i, 0] = sigma
        else:
            out[i, 0] = float(fr.quality.noise_sigma or 1.0)
        psf = fr.psf
        out[i, 1] = float(psf.fwhm_pixels or 0.0) if psf else 0.0
        out[i, 2] = float(psf.eccentricity or 0.0) if psf else 0.0
        out[i, 3] = float(fr.quality.airmass or 1.0)
        out[i, 4] = float(fr.quality.zero_point or 0.0)
        out[i, 5] = float(fr.quality.background_adu or 0.0)
        out[i, 6] = 1.0 if fr.meta.photometrically_unreliable else 0.0
    return out


_MODULE_CACHE: dict[str, Any] = {}


def _build_modules() -> dict[str, Any]:
    """Define the torch modules on first use.

    Everything torch lives inside this function so that importing
    :mod:`astrostack.sr.wcs_burst` costs nothing when torch is absent.
    """
    if _MODULE_CACHE:
        return _MODULE_CACHE
    t = torch()
    nn = t.nn
    fn = t.nn.functional

    class ResidualBlock(nn.Module):
        def __init__(self, channels: int) -> None:
            super().__init__()
            self.conv1 = nn.Conv2d(channels, channels, 3, padding=1)
            self.conv2 = nn.Conv2d(channels, channels, 3, padding=1)

        def forward(self, x):
            y = fn.gelu(self.conv1(x))
            return x + self.conv2(y)

    class ConditioningEncoder(nn.Module):
        """Turns the 7 physical channels into a feature modulation."""

        def __init__(self, in_channels: int, out_channels: int) -> None:
            super().__init__()
            self.net = nn.Sequential(
                nn.Conv2d(in_channels, out_channels, 1),
                nn.GELU(),
                nn.Conv2d(out_channels, 2 * out_channels, 1),
            )

        def forward(self, cond):
            gamma_beta = self.net(cond)
            gamma, beta = gamma_beta.chunk(2, dim=1)
            return 1.0 + gamma, beta

    class RecurrentFusionCell(nn.Module):
        """Gated update of the persistent scene state by one more frame.

        This is the RBSR property that matters here: fold frames in one at a
        time so N can be anything, with the base frame acting as a standing
        prompt.
        """

        def __init__(self, channels: int) -> None:
            super().__init__()
            self.gate = nn.Conv2d(3 * channels, channels, 3, padding=1)
            self.update = nn.Conv2d(3 * channels, channels, 3, padding=1)
            self.refine = ResidualBlock(channels)

        def forward(self, state, feat, prompt):
            joint = t.cat([state, feat, prompt], dim=1)
            g = t.sigmoid(self.gate(joint))
            u = t.tanh(self.update(joint))
            return self.refine(state * (1.0 - g) + u * g)

    class WCSBurstNet(nn.Module):
        """The whole Tier B model, minus the alignment stage we deleted."""

        def __init__(
            self,
            channels: int = 64,
            n_blocks: int = 6,
            scale: int = 2,
            cond_channels: int = 7,
        ) -> None:
            super().__init__()
            self.scale = int(scale)
            self.head = nn.Conv2d(2, channels, 3, padding=1)  # data + validity
            self.cond = ConditioningEncoder(cond_channels, channels)
            self.encoder = nn.Sequential(*[ResidualBlock(channels) for _ in range(n_blocks // 2)])
            self.cell = RecurrentFusionCell(channels)
            self.body = nn.Sequential(*[ResidualBlock(channels) for _ in range(n_blocks)])
            self.upsample = nn.Sequential(
                nn.Conv2d(channels, channels * self.scale * self.scale, 3, padding=1),
                nn.PixelShuffle(self.scale),
                nn.GELU(),
            )
            self.to_image = nn.Conv2d(channels, 1, 3, padding=1)
            self.to_logvar = nn.Conv2d(channels, 1, 3, padding=1)

        def encode(self, frame, valid, cond):
            x = self.head(t.cat([frame, valid], dim=1))
            gamma, beta = self.cond(cond)
            return self.encoder(x * gamma + beta)

        def forward(self, frames, valid, cond, base_index: int = 0):
            """``frames``: (B, N, 1, H, W). Returns ``(image, logvar)``."""
            b, n = frames.shape[0], frames.shape[1]
            prompt = self.encode(frames[:, base_index], valid[:, base_index], cond[:, base_index])
            state = prompt
            for j in range(n):
                if j == base_index:
                    continue
                feat = self.encode(frames[:, j], valid[:, j], cond[:, j])
                state = self.cell(state, feat, prompt)
            body = self.body(state)
            up = self.upsample(body)
            image = self.to_image(up)
            logvar = self.to_logvar(up)
            _ = b
            return image, logvar

    _MODULE_CACHE.update(
        {
            "ResidualBlock": ResidualBlock,
            "ConditioningEncoder": ConditioningEncoder,
            "RecurrentFusionCell": RecurrentFusionCell,
            "WCSBurstNet": WCSBurstNet,
        }
    )
    return _MODULE_CACHE


@register_resolver("wcs-burst")
class WCSBurstSR(SuperResolver):
    """RBSR-style recurrent burst SR with WCS warping and physical conditioning."""

    def __init__(
        self,
        scale: float = 2.0,
        device: str | None = None,
        channels: int = 64,
        n_blocks: int = 6,
        weights: str | None = None,
        allow_untrained: bool = False,
    ) -> None:
        super().__init__(scale=scale, device=device)
        self.channels = int(channels)
        self.n_blocks = int(n_blocks)
        self.allow_untrained = bool(allow_untrained)
        self._net: Any = None
        self._weights_path: str | None = None
        if weights:
            self.load_weights(weights)

    # -- torch plumbing ---------------------------------------------------
    def _ensure_net(self) -> Any:
        if self._net is not None:
            return self._net
        mods = _build_modules()
        t = torch()
        device = self.device or select_device()
        net = mods["WCSBurstNet"](
            channels=self.channels, n_blocks=self.n_blocks, scale=round(self.scale)
        )
        net.to(t.device(device))
        net.eval()
        self.device = device
        self._net = net
        return net

    def load_weights(self, path: str) -> None:
        t = torch()
        net = self._ensure_net()
        state = t.load(path, map_location=self.device or "cpu", weights_only=True)
        if isinstance(state, dict) and "model" in state:
            state = state["model"]
        net.load_state_dict(state)
        net.eval()
        self._weights_path = str(path)

    def build_network(self) -> Any:
        """Expose the raw ``nn.Module`` for the training loop."""
        return self._ensure_net()

    # -- inference --------------------------------------------------------
    def enhance(self, inputs: SRInputs) -> SRResult:
        """Run the model. Requires torch *and* trained weights."""
        if self._weights_path is None and not self.allow_untrained:
            raise AstroStackError(
                "WCSBurstSR has no trained weights. Tier B is an optional enhancement layer "
                "over the Tier A coadd, and an untrained network would emit structure that is "
                "pure prior. Pass weights=<path to the .pt from the models table>, or set "
                "allow_untrained=True for a smoke test whose output must not be published."
            )
        grid = inputs.output_grid
        if grid is None:
            raise AstroStackError("WCSBurstSR needs inputs.output_grid (the Tier A output grid)")

        t = torch()
        net = self._ensure_net()
        data, valid, _ = wcs_warp_stack(inputs, grid)
        cond = build_condition_channels(inputs, data.shape[1:])

        dev = t.device(self.device or "cpu")
        frames_t = t.from_numpy(data[None, :, None]).to(dev)
        valid_t = t.from_numpy(valid[None, :, None]).to(dev)
        cond_t = t.from_numpy(cond[None]).to(dev)

        # Normalise by the robust scale of the base frame so the network sees
        # a consistent dynamic range regardless of the corpus's zero points.
        scale_norm = float(np.median(np.abs(data[0])) * 1.4826) or 1.0
        frames_t = frames_t / scale_norm

        with t.no_grad():
            image, logvar = net(frames_t, valid_t, cond_t)
        img = image[0, 0].detach().cpu().numpy().astype(np.float32) * scale_norm
        sigma = (
            np.exp(0.5 * logvar[0, 0].detach().cpu().numpy()).astype(np.float32) * scale_norm
        )

        notes = [
            "WCS-driven warping replaced the learned alignment module: geometry is analytic, "
            "not inferred",
            "recurrent fusion over N frames; N may differ between runs without retraining",
            "physical conditioning: sigma map, PSF FWHM/ellipticity, airmass, zero point, "
            "background, photometric-reliability flag",
        ]
        if self._weights_path is None:
            notes.append(
                "UNTRAINED NETWORK: this output is meaningless and must never be published"
            )

        return SRResult(
            image=img,
            uncertainty=sigma,
            architecture=self.architecture,
            scale=self.scale,
            model_id=self._weights_path,
            baseline=inputs.reference_coadd,
            metrics={
                "n_frames": len(inputs),
                "device": self.device,
                "normalisation": scale_norm,
                "trained": self._weights_path is not None,
            },
            notes=notes,
        )
