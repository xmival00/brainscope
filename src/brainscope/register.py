"""Rigid CT-to-MRI coregistration (mutual information, coarse to fine)."""
from __future__ import annotations

import numpy as np
import SimpleITK as sitk

from .transforms import apply, ras_to_sitk, sitk_to_ras, to_sitk

# Smoothing (mm) per resolution level and image shrink factors. Pig brains are smaller: finer smoothing.
SPECIES = {
    "human": {"shrink": [4, 2, 1], "smooth_mm": [3.0, 1.5, 0.0]},
    "pig": {"shrink": [4, 2, 1], "smooth_mm": [2.0, 1.0, 0.0]},
    "other": {"shrink": [4, 2, 1], "smooth_mm": [2.0, 1.0, 0.0]},
}
CT_WINDOW = (-200.0, 1500.0)  # HU kept for the metric; metal is masked out separately
SAMPLES = 300_000


def coregister(ct_data, ct_affine, mri_data, mri_affine, species="human", init=None,
               crop_center=None, crop_radius=None, metal_hu=2500.0, iterations=300, seed=1):
    """Return (4x4 CT world -> MRI world, final metric). Affines are the orientation-corrected ones.

    `init` is a starting CT->MRI 4x4 (e.g. after a manual nudge); otherwise the image centers of mass are
    aligned. `crop_center`/`crop_radius` (MRI world, mm) restrict the metric to a box around the head/brain.
    """
    p = SPECIES.get(species, SPECIES["other"])
    fixed = to_sitk(mri_data, mri_affine)
    moving_raw = to_sitk(ct_data, ct_affine)
    moving = sitk.Clamp(moving_raw, sitk.sitkFloat32, *CT_WINDOW)

    reg = sitk.ImageRegistrationMethod()
    reg.SetMetricAsMattesMutualInformation(numberOfHistogramBins=50)
    reg.SetMetricSamplingStrategy(reg.RANDOM)
    n_vox = float(np.prod(fixed.GetSize()))
    reg.SetMetricSamplingPercentagePerLevel(  # about SAMPLES voxels per level: noise-free enough, still fast
        [min(1.0, SAMPLES * f ** 3 / n_vox) for f in p["shrink"]], seed)
    reg.SetInterpolator(sitk.sitkLinear)
    reg.SetOptimizerAsRegularStepGradientDescent(learningRate=2.0, minStep=1e-4, numberOfIterations=iterations,
                                                 relaxationFactor=0.7, gradientMagnitudeTolerance=1e-10)
    reg.SetOptimizerScalesFromPhysicalShift()
    reg.SetShrinkFactorsPerLevel(p["shrink"])
    reg.SetSmoothingSigmasPerLevel(p["smooth_mm"])
    reg.SmoothingSigmasAreSpecifiedInPhysicalUnitsOn()

    metal = moving_raw > metal_hu
    if sitk.GetArrayViewFromImage(metal).any():
        metal = sitk.BinaryDilate(metal, [2, 2, 2])
    reg.SetMetricMovingMask(sitk.Cast(1 - metal, sitk.sitkUInt8))
    if crop_center is not None and crop_radius:
        reg.SetMetricFixedMask(_box_mask(fixed, mri_affine, crop_center, crop_radius))

    if init is not None:
        tx = ras_to_sitk(np.linalg.inv(init), center=fixed.TransformContinuousIndexToPhysicalPoint(
            [(s - 1) / 2 for s in fixed.GetSize()]))
    else:
        tx = sitk.CenteredTransformInitializer(
            sitk.Clamp(fixed, sitk.sitkFloat32, 0, float(np.percentile(mri_data, 99.5))),
            sitk.Clamp(moving, sitk.sitkFloat32, 0, CT_WINDOW[1]),
            sitk.Euler3DTransform(), sitk.CenteredTransformInitializerFilter.MOMENTS)
    reg.SetInitialTransform(tx, inPlace=True)
    reg.Execute(fixed, moving)
    mri_to_ct = sitk_to_ras(tx)
    return np.linalg.inv(mri_to_ct), float(reg.GetMetricValue())


def _box_mask(fixed, mri_affine, center, radius):
    shape = fixed.GetSize()  # (i, j, k)
    inv = np.linalg.inv(mri_affine)
    corners = np.array([[center[0] + sx * radius, center[1] + sy * radius, center[2] + sz * radius]
                        for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)])
    vox = apply(inv, corners)
    lo = np.clip(np.floor(vox.min(0)).astype(int), 0, np.array(shape) - 1)
    hi = np.clip(np.ceil(vox.max(0)).astype(int), 0, np.array(shape) - 1)
    m = np.zeros(shape[::-1], np.uint8)  # sitk array order is [k, j, i]
    m[lo[2]:hi[2] + 1, lo[1]:hi[1] + 1, lo[0]:hi[0] + 1] = 1
    img = sitk.GetImageFromArray(m)
    img.CopyInformation(fixed)
    return img


def mean_displacement(m1, m2, center, radius=40.0):
    """Mean distance (mm) between two transforms over points in a sphere: a target-error proxy."""
    rng = np.random.default_rng(0)
    pts = rng.normal(size=(2000, 3))
    pts = center + radius * pts / np.linalg.norm(pts, axis=1, keepdims=True) * rng.random((2000, 1)) ** (1 / 3)
    return float(np.linalg.norm(apply(m1, pts) - apply(m2, pts), axis=1).mean())
