"""
Display: one preview window per camera, in that camera's process.

The grab thread puts every frameRate/displayFrameRate-th frame, downsampled
by displayDownsample, in dispQueue (DisplayImage in campy/cameras/); this
shows the newest in a plain Qt window, scaled to fit it. A mono frame is
shown as gray as it comes, a color frame as RGB.

The preview used to be a matplotlib figure, and every frame it showed went
through matplotlib's image drawing: 33 ms of CPU per frame on the
ChronicRecord PC (Core Ultra 9 285), 21 ms on an M-series Mac, so at
displayFrameRate 10 every camera spent a fifth to a third of a CPU core on
its preview. Qt draws the same frame, 640x512, in 0.2 ms.

DisplayFrames runs in its own thread (campy.ShowPreview), so Qt's
application object is made, and its events run, in that thread. Import this
module in that thread too: Qt takes the first thread that loads it as its
main thread, and the QApplication has to be made in that one.
"""
import sys, time
import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
from campy.utils.utils import Say


def ToQImage(img):
	"""
	A QImage of a frame, and the array it reads its pixels from: the QImage
	does not copy them, so keep the array as long as the QImage. Mono
	(height x width) is gray, height x width x 3 is RGB. Frames deeper than
	8 bits are scaled so the brightest pixel is white.
	"""
	img = np.asarray(img)
	if img.ndim == 3 and img.shape[2] == 1:
		img = img[:, :, 0]
	if img.dtype != np.uint8:
		top = float(img.max()) if img.size else 0.0
		img = (img * (255.0 / top) if top > 0 else np.zeros(img.shape)).astype(np.uint8)
	img = np.ascontiguousarray(img) # a downsampled frame is a strided view
	height, width = img.shape[:2]
	if img.ndim == 2:
		fmt = QtGui.QImage.Format_Grayscale8
	elif img.shape[2] == 3:
		fmt = QtGui.QImage.Format_RGB888
	elif img.shape[2] == 4:
		fmt = QtGui.QImage.Format_RGBA8888
	else:
		raise ValueError("cannot display a frame of shape {}".format(img.shape))
	return QtGui.QImage(img.data, width, height, img.strides[0], fmt), img


class PreviewWindow(QtWidgets.QWidget):
	"""A window that shows the latest frame, as large as fits, on black."""

	def __init__(self, title):
		super().__init__()
		self.setWindowTitle(title)
		self.image, self.pixels = None, None
		self.setAttribute(QtCore.Qt.WA_OpaquePaintEvent) # paintEvent covers every pixel

	def ShowFrame(self, img):
		first = self.image is None
		self.image, self.pixels = ToQImage(img)
		if first:
			self.resize(self.image.width(), self.image.height())
		self.update()

	def FrameRect(self):
		# Where the frame is drawn: centred, scaled to fit, aspect ratio kept
		size = self.image.size().scaled(self.size(), QtCore.Qt.KeepAspectRatio)
		rect = QtCore.QRect(QtCore.QPoint(0, 0), size)
		rect.moveCenter(self.rect().center())
		return rect

	def paintEvent(self, event):
		painter = QtGui.QPainter(self)
		painter.fillRect(self.rect(), QtCore.Qt.black)
		if self.image is not None:
			painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
			painter.drawImage(self.FrameRect(), self.image)
		painter.end()


def DisplayFrames(cam_params, dispQueue):
	if sys.platform == "win32" and cam_params["cameraMake"] == "basler":
		return # Basler cameras use Pylon's own image window (cameras/basler.py)

	app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
	window = PreviewWindow(cam_params["cameraName"])
	step = max(1, int(cam_params.get("displayDownsample", 1)))
	window.resize(int(cam_params["frameWidth"]) // step, int(cam_params["frameHeight"]) // step)
	window.show()
	failed = False
	while True:
		if dispQueue:
			img = dispQueue.popleft()
			if isinstance(img, str): # "STOP": the camera has stopped grabbing
				break
			try:
				window.ShowFrame(img)
			except Exception as e:
				# The recording goes on; say so once rather than at every frame
				if not failed:
					Say("{}: the preview cannot show its frames: {}".format(cam_params["cameraName"], e))
				failed = True
		else:
			time.sleep(0.01)
		app.processEvents()
	window.close()
	app.processEvents()
