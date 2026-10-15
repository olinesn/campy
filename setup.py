from setuptools import setup, find_packages

setup(
	name='campy',
	version='2.0.1',
	packages=find_packages(),
	install_requires=[
					'imageio',
					'imageio-ffmpeg',
					'matplotlib',
					'numpy',
					'pyserial',
					'pyyaml',
					'psutil',
					'scikit-image',
					'PyQt5',
					],
	extras_require={
		# RatCam (cameraMake "ratcam"): the host package of nir-eye-cam
		"ratcam": ["nireyecam @ git+https://github.com/olinesn/nir-eye-cam.git#subdirectory=host"],
	},
	entry_points={
		"console_scripts": [
			"campy-acquire = campy.campy:Main",
			"campy-encode-sweep = campy.sweep:Main",
			"campy-encode-quality = campy.quality:Main",
			"campy-align = campy.align:Main",
			"campy-synctest = campy.synctest:Main",
		]
	}
)
