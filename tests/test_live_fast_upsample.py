"""
campy-live --fast-upsample (campy/live.py: LayoutUpsample): the models'
bilinear upsampling layers run in the other memory layout where that was
timed faster, with the same results. On the CPU: the layer's two ways agree
and the layers are found and put back; with a GPU (CUDA or ROCm): the
random models' keypoints are the same with it on and off, also with
--cuda-graphs and --fixed-crops. Needs torch (and sleap-nn for the models).
"""
import argparse

import numpy as np
import pytest

from campy import live

torch = pytest.importorskip("torch")

GPU = torch.cuda.is_available()
needs_gpu = pytest.mark.skipif(not GPU, reason="needs a CUDA (or ROCm) GPU")
SHAPE = "256x320x1"


class Net(torch.nn.Module):
	def __init__(self):
		super().__init__()
		self.blocks = torch.nn.ModuleDict({"up": torch.nn.Upsample(scale_factor=2, mode="bilinear",
			align_corners=False), "near": torch.nn.Upsample(scale_factor=2, mode="nearest")})

	def forward(self, x):
		return self.blocks["near"](self.blocks["up"](x))


@pytest.mark.parametrize("layout", [torch.contiguous_format, torch.channels_last])
def test_other_layout_same_output_and_layout(layout):
	up = torch.nn.Upsample(scale_factor=2, mode="bilinear", align_corners=False)
	wrapped = live.LayoutUpsample(up, None, None)
	x = torch.randn(3, 64, 5, 5).contiguous(memory_format=layout)
	want, got = up(x), wrapped.Other(x)
	assert torch.equal(want, got)
	assert got.stride() == want.stride() # the next layer gets the layout it always gets


def test_into_finds_bilinear_only_and_settle_puts_back():
	net = Net()
	ups = live.LayoutUpsample.Into(net)
	assert len(ups) == 1 and net.blocks["up"] is ups[0] and type(net.blocks["near"]) is torch.nn.Upsample
	x = torch.randn(2, 8, 4, 4)
	assert torch.equal(net(x), Net()(x)) # nothing timed (not measuring): as is
	ups[0].timed[("shape",)] = (0.1, 0.2, False)
	ups[0].Settle() # faster as is everywhere: the plain layer goes back
	assert type(net.blocks["up"]) is torch.nn.Upsample
	net = Net()
	ups = live.LayoutUpsample.Into(net)
	ups[0].timed[("shape",)] = (0.2, 0.1, True)
	ups[0].Settle()
	assert net.blocks["up"] is ups[0]


def test_auto():
	def On(value, device, fmt):
		return live.FastUpsample(argparse.Namespace(fast_upsample=value), device, fmt)
	assert On("auto", "cuda", "nchw") and not On("auto", "cuda", "channels_last")
	assert On("on", "cuda", "channels_last") and not On("off", "cuda", "nchw")
	assert not On("on", "cpu", "nchw") and not On("auto", "mps", "nchw")
	assert live.ParseArgs([]).fast_upsample == "auto"


def Keypoints(fast, more):
	pytest.importorskip("sleap_nn")
	args = live.ParseArgs(["--model", "random", "--frame-shape", SHAPE, "--memory-format", "nchw",
		"--max-instances", "3", "--animals", "3", "--fast-upsample", fast] + more)
	model = live.MODELS["random"](args, 2, live.FrameShape(SHAPE))
	host = live.HostBuffer(True, (2,) + live.FrameShape(SHAPE), np.uint8)
	host.array[:] = np.random.default_rng(1).integers(0, 255, host.array.shape, dtype=np.uint8)
	model.Warm(host.tensor)
	out = [model.Predict(model.Upload(host.tensor))["keypoints"] for _ in range(3)]
	return model, out


@needs_gpu
@pytest.mark.parametrize("more", [[], ["--cuda-graphs"], ["--fixed-crops", "--cuda-graphs"]])
def test_same_keypoints_on_gpu(more):
	torch.manual_seed(0)
	_, off = Keypoints("off", more)
	torch.manual_seed(0)
	model, on = Keypoints("on", more)
	assert len(model.upsamples) > 0 and all(up.timed for up in model.upsamples)
	assert model.DescribeUpsamples().startswith("--fast-upsample: ")
	for a, b in zip(off, on):
		np.testing.assert_array_equal(a, b)
