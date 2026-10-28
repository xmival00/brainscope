# Third-party software

brainscope is licensed under Apache-2.0 (see `LICENSE`). The installers bundle the following software, each under
its own license:

| Component | Use | License |
|---|---|---|
| [NiiVue](https://github.com/niivue/niivue) 0.69.0 (vendored in `src/brainscope/web/vendor/niivue.umd.js`) | image viewer in the UI | BSD 2-Clause |
| [dcm2niix](https://github.com/rordenlab/dcm2niix) (via the `dcm2niix` Python package) | DICOM to NIfTI | BSD-style (see the dcm2niix repository) |
| [SimpleITK](https://simpleitk.org) | registration, resampling | Apache-2.0 |
| [NiBabel](https://nipy.org/nibabel) | NIfTI files | MIT |
| [NumPy](https://numpy.org), [SciPy](https://scipy.org) | numerics | BSD 3-Clause |
| [pywebview](https://pywebview.flowrl.com) | native window | BSD 3-Clause |
| [PyInstaller](https://pyinstaller.org) bootloader | packaging | GPL-2.0 with the bootloader exception |
