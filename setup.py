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
					'scikit-image',
					'PyQt5',
					],
	entry_points={
		"console_scripts": [
			"campy-acquire = campy.campy:Main",
			"campy-encode-sweep = campy.sweep:Main",
		]
	}
)
