"""
campy-live and campy-infer need sleap-nn 0.3.5 or later (campy/live.py:
SleapProblem), which has the two fixes campy used to patch in itself: every
crop cut at the model's crop size (talmolab/sleap-nn#780; 0.3.3 cut a set's
crops a pixel short for some centroids, #778) and tied centroid peaks kept
(#782). Needs torch and sleap-nn; runs on the CPU.
"""
import pytest

from campy import live

torch = pytest.importorskip("torch")
pytest.importorskip("sleap_nn")

# A centroid sleap-nn 0.3.3 cropped 192 x 191 around, with 192 px crops
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


@pytest.fixture
def sleap():
	if live.SleapProblem():
		pytest.skip(live.SleapProblem())


def test_sleap_cuts_crop_size(sleap):
	# sleap-nn itself, with no help from campy: the crop size, also when its
	# float corners would have rounded down (the box alone, without the size)
	from sleap_nn.inference.ops.crops import crop_bboxes, make_centered_bboxes
	boxes = make_centered_bboxes(torch.tensor([CENTROID]), 192, 192)
	assert tuple(crop_bboxes(torch.zeros(1, 1, 256, 320), boxes, torch.tensor([0])).shape[-2:]) == (192, 192)
	assert CropShape(Model().layer) == (192, 192)
	assert "_run_stage_2" not in vars(Model().layer) # left as sleap-nn made it


def test_an_old_sleap_is_refused(monkeypatch):
	# a crop_bboxes without crop_size= is 0.3.4 or older: campy-live says so,
	# and how to update, rather than giving a set of short crops
	from sleap_nn.inference.ops import crops

	def Old(images, bboxes, sample_inds):
		raise AssertionError("not to be called")

	monkeypatch.setattr(crops, "crop_bboxes", Old)
	problem = live.SleapProblem()
	assert problem and "0.3.5" in problem and "pip install" in problem
	with pytest.raises(SystemExit, match="0.3.5"):
		Model()


def test_strict_peaks_is_gone():
	with pytest.raises(SystemExit, match="strict-peaks"):
		live.ParseArgs(["--model", "random", "--strict-peaks"])
