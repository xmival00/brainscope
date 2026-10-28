import numpy as np
import SimpleITK as sitk

from brainscope import dicom, phantom, volume
from brainscope.transforms import apply, rotation, to_sitk


def _write_series(data, affine, folder):
    """A minimal CT DICOM series (one file per slice), as a scanner would write it."""
    folder.mkdir()
    img = sitk.Cast(to_sitk(data, affine), sitk.sitkInt16)
    d = img.GetDirection()
    w = sitk.ImageFileWriter()
    w.KeepOriginalImageUIDOn()
    common = {"0008|0060": "CT", "0008|103e": "test head", "0020|000d": "1.2.826.0.1.3680043.2.1125.1",
              "0020|000e": "1.2.826.0.1.3680043.2.1125.2", "0020|0011": "3", "0010|0010": "Test^Subject",
              "0020|0037": "\\".join(f"{x:.6f}" for x in (d[0], d[3], d[6], d[1], d[4], d[7])),
              "0028|0030": f"{img.GetSpacing()[1]:.4f}\\{img.GetSpacing()[0]:.4f}",
              "0018|0050": f"{img.GetSpacing()[2]:.4f}", "0028|1052": "0", "0028|1053": "1",
              "0020|0052": "1.2.826.0.1.3680043.2.1125.4", "0018|5100": "HFS", "0008|0070": "TEST",
              "0008|0008": "ORIGINAL\\PRIMARY\\AXIAL", "0008|0021": "20260101", "0008|0031": "120000",
              "0008|0020": "20260101", "0008|0030": "120000", "0020|0012": "1"}
    for k in range(img.GetDepth()):
        sl = img[:, :, k]
        for tag, val in common.items():
            sl.SetMetaData(tag, val)
        sl.SetMetaData("0020|0032", "\\".join(f"{x:.4f}" for x in img.TransformIndexToPhysicalPoint((0, 0, k))))
        sl.SetMetaData("0020|0013", str(k + 1))
        sl.SetMetaData("0008|0018", f"1.2.826.0.1.3680043.2.1125.3.{k + 1}")
        w.SetFileName(str(folder / f"{k:04d}.dcm"))
        w.Execute(sl)


def test_dicom_series_converts_with_the_right_geometry(tmp_path):
    data, aff = phantom.head(spacing=4.0, contrast="ct")
    oblique = np.eye(4)
    oblique[:3, :3] = rotation((0, 0, 20))  # in-plane rotation, as on a tilted scan
    aff = oblique @ aff
    _write_series(data, aff, tmp_path / "dcm")
    series = dicom.convert(tmp_path / "dcm", tmp_path / "out")
    assert len(series) == 1 and series[0]["modality"] == "CT"
    assert "Test" not in (tmp_path / "out" / series[0]["file"].replace(".nii.gz", ".json")).read_text()
    v = volume.load(tmp_path / "out" / series[0]["file"])

    def bone_center(d, a):
        ijk = np.argwhere(d > 500)
        return apply(a, ijk).mean(0)

    assert np.linalg.norm(bone_center(v.data, v.affine) - bone_center(data, aff)) < 0.5
    assert sorted(v.data.shape) == sorted(data.shape)
