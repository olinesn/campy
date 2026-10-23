from setuptools import setup, find_packages

setup(
	name='campy',
	version='2.1.1rc1',
	packages=find_packages(),
	python_requires='>=3.9',
	install_requires=[
					'imageio',
					'imageio-ffmpeg',
					'numpy',
					'pyserial',
					'pyyaml',
					'scipy',
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
			"campy-config = campy.configtool:Main",
			"campy-encode-sweep = campy.sweep:Main",
			"campy-encode-quality = campy.quality:Main",
			"campy-align = campy.align:Main",
			"campy-synctest = campy.synctest:Main",
			"campy-flir-reset = campy.flirreset:Main",
			"campy-live = campy.live:Main",
			"campy-live-view = campy.liveview:Main",
			"campy-live-export = campy.liveexport:Main",
		]
	}
)
