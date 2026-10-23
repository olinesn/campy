"""
campy-live's own sleap-nn TopDownLayers cut their crops at the crop size
(campy/live.py: ExactCropSize). sleap-nn 0.3.3's crop_bboxes takes the size
from the first box's float32 corners, and for some centroids cuts every crop
of the set a pixel short. talmolab/sleap-nn#780 (after 0.3.4) fixed it, and
campy-live then leaves sleap-nn as it is; these tests check whichever is
installed. Needs torch and sleap-nn; runs on the CPU.
"""
import pytest

from campy import live

torch = pytest.importorskip("torch")
pytest.importorskip("sleap_nn")

# A centroid sleap-nn crops 192 x 191 around, with 192 px crops
CENTROID = [221.54798889160156, 77.36744689941406]


def Model():
	args = live.ParseArgs(["--model", "random", "--device", "cpu", "--frame-shape", "256x320x1",
		"--max-instances", "1", "--animals", "1", "--nodes", "3", "--crop", "192"])
	return live.MODELS["random"](args, 1, live.FrameShape("256x320x1"))


def CropShape(layer):
	# The crops the layer's instance model gets, for one animal at CENTROID
	seen = []
	instance = layer.centered_instance_layer
	predict = instance.predict

	def Spy(crops):
		seen.append(tuple(crops.shape[-2:]))
		return predict(crops)

	instance.predict = Spy
	try:
		layer._run_stage_2(torch.zeros(1, 1, 256, 320, dtype=torch.uint8), torch.tensor([[CENTROID]]),
			torch.ones(1, 1), torch.ones(1, 1, dtype=torch.bool))
	finally:
		instance.predict = predict
	return seen[0]


def SleapCrop():
	# sleap-nn's own crop_bboxes, as installed, for one animal at CENTROID
	from sleap_nn.inference.ops.crops import crop_bboxes, make_centered_bboxes
	boxes = make_centered_bboxes(torch.tensor([CENTROID]), 192, 192)
	return tuple(crop_bboxes(torch.zeros(1, 1, 256, 320), boxes, torch.tensor([0])).shape[-2:])


FIXED = "this sleap-nn cuts crops at their size itself (talmolab/sleap-nn#780), and campy-live leaves it as it is; " \
	"ExactCropSize can go once campy needs a sleap-nn with the fix"


def test_sleap_cuts_short():
	# The bug this works around, and campy-live telling whether the
	# installed sleap-nn has it
	assert SleapCrop() == ((192, 191) if live.SleapCutsShort() else (192, 192))


def test_default_cuts_crop_size():
	assert CropShape(Model().layer) == (192, 192)


def test_other_layers_untouched():
	# A TopDownLayer campy-live didn't build still crops as sleap-nn does
	from sleap_nn.inference.layers.topdown import TopDownLayer
	ours = Model().layer
	other = TopDownLayer(centroid_layer=ours.centroid_layer, centered_instance_layer=ours.centered_instance_layer,
		crop_size=(192, 192))
	assert CropShape(other) == SleapCrop()
	assert CropShape(ours) == (192, 192)


def test_steps_aside_for_a_fixed_sleap(monkeypatch):
	# A sleap-nn whose crop_bboxes takes crop_size= cuts that size, and
	# rounds the size it works out: ExactCropSize changes nothing there
	from sleap_nn.inference.layers import topdown
	from sleap_nn.inference.ops import crops
	original = crops.crop_bboxes

	def Fixed(images, bboxes, sample_inds, crop_size=None):
		return original(images, bboxes, sample_inds)

	monkeypatch.setattr(crops, "crop_bboxes", Fixed)
	monkeypatch.setattr(topdown, "crop_bboxes", Fixed)
	assert not live.SleapCutsShort()
	model = Model()
	assert topdown.crop_bboxes is Fixed
	assert "_run_stage_2" not in vars(model.layer) # not wrapped


def test_keypoints_placed_as_before():
	# Only the crop's size changes: where it is cut, and so where its
	# keypoints land in the frame, is sleap-nn's top-left, as before
	if not live.SleapCutsShort():
		pytest.skip(FIXED)
	from sleap_nn.inference.layers import topdown
	from sleap_nn.inference.ops.crops import crop_bboxes, make_centered_bboxes
	Model() # puts ExactCropSize's crop_bboxes into topdown
	image = torch.arange(256 * 320, dtype=torch.float32).view(1, 1, 256, 320)
	boxes = make_centered_bboxes(torch.tensor([CENTROID]), 192, 192)
	short = crop_bboxes(image, boxes, torch.tensor([0]))
	live._exactCrop.size = (192, 192)
	try:
		exact = topdown.crop_bboxes(image, boxes, torch.tensor([0]))
	finally:
		live._exactCrop.size = None
	assert exact.shape[-2:] == (192, 192)
	assert torch.equal(exact[..., :191], short)
	assert torch.equal(topdown.crop_bboxes(image, boxes, torch.tensor([0])), short) # outside our layers
