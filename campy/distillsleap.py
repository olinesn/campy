"""
campy-distill-train's sleap-nn side (campy/distill.py says what it does): a
CenteredInstanceDataset that also cuts the teacher's crop, the teacher and its
maps on the student's grid, and a training step with the teacher's term.

Imported only by campy-distill-train and by spawned data-loader workers
(which find the dataset class here by name). Needs torch and sleap-nn 0.3.5
or later. Install() swaps these classes into sleap-nn's own factories, so
`sleap-nn train` builds them; nothing in sleap-nn is changed on disk.
"""

import os

import torch
import torch.nn.functional as F
from omegaconf import OmegaConf

import sleap_nn
import sleap_nn.data.custom_datasets as cd
import sleap_nn.training.lightning_modules as lm
from sleap_nn.data.instance_cropping import make_centered_bboxes
from sleap_nn.data.normalization import normalize_on_gpu
from sleap_nn.data.resizing import apply_pad_to_stride

from campy import distill

_Dataset = cd.CenteredInstanceDataset
_Module = lm.TopDownCenteredInstanceLightningModule
if not (hasattr(_Dataset, "_final_crop") and hasattr(_Dataset, "_crop_for_augmentation")):
	raise distill.DistillError("sleap-nn {} is too old: campy-distill-train needs 0.3.5 or later".format(sleap_nn.__version__))


def LoadConfig(run):
	for name in ("training_config.yaml", "initial_config.yaml"):
		path = os.path.join(run, name)
		if os.path.exists(path):
			return OmegaConf.load(path)
	raise distill.DistillError("no training_config.yaml or initial_config.yaml in " + run)


def TeacherCrop():
	if os.environ.get(distill.ENV_CROP):
		return int(os.environ[distill.ENV_CROP])
	run = os.environ.get(distill.ENV_TEACHER)
	if not run or float(os.environ.get(distill.ENV_ALPHA, "0")) <= 0:
		return 0
	return int(LoadConfig(run).data_config.preprocessing.crop_size)


class DistillInstanceDataset(_Dataset):
	"""Also returns "teacher_image": a teacher-sized crop of the same augmented
	image around the same centre (fixed crops only; box-scaled crops pass)."""

	def _teacher_crop(self):
		if not hasattr(self, "_campy_tcrop"):
			self._campy_tcrop = TeacherCrop()
		return self._campy_tcrop

	def _crop_for_augmentation(self, image, instance, centroid, box, eff_scale):
		T = self._teacher_crop()
		if self.bbox_crop is not None or T <= self.crop_size:
			return super()._crop_for_augmentation(image, instance, centroid, box, eff_scale)
		# sleap-nn's fixed-crop branch with the bigger crop: rotation and scaling
		# are about this pre-crop's centre (the centroid), so the student's final
		# crop is the same, with less black in its corners
		m = int(T * 2 ** 0.5)
		return cd.generate_crops(image, instance, centroid, [m, m])

	def _final_crop(self, sample):
		T = self._teacher_crop()
		if self.bbox_crop is None and T > 0:
			box = make_centered_bboxes(sample["centroid"][0], T, T)[None]
			sample["teacher_image"] = cd.crop_and_resize(sample["instance_image"], boxes=box, size=(T, T))
		return super()._final_crop(sample)


def _Gaussian(sigma, device):
	r = max(1, int(3 * sigma + 0.999))
	x = torch.arange(-r, r + 1, device=device, dtype=torch.float32)
	k = torch.exp(-0.5 * (x / sigma) ** 2)
	return k / k.sum(), r


def ToStudentGrid(y, out_hw, student_stride, student_scale, offset, teacher_stride, blur, peak_floor=0.0):
	"""Teacher maps y (B, N, Ht, Wt) -> (B, N, h, w) on the student's output
	grid: blurred by `blur` teacher output px keeping each map's peak height,
	peaks >= peak_floor rescaled to 1 (0: off), sampled bilinearly."""
	y = y.float().clamp_min(0)
	if blur > 0:
		peak = y.amax(dim=(2, 3), keepdim=True)
		k, r = _Gaussian(blur, y.device)
		B, C, H, W = y.shape
		z = y.reshape(B * C, 1, H, W)
		z = F.conv2d(F.pad(z, (r, r, 0, 0), mode="replicate"), k.view(1, 1, 1, -1))
		z = F.conv2d(F.pad(z, (0, 0, r, r), mode="replicate"), k.view(1, 1, -1, 1))
		z = z.reshape(B, C, H, W)
		y = z * (peak / z.amax(dim=(2, 3), keepdim=True).clamp_min(1e-6))
	if peak_floor > 0:
		peak = y.amax(dim=(2, 3), keepdim=True)
		y = torch.where(peak >= peak_floor, y / peak.clamp_min(1e-6), y)
	h, w = out_hw
	Ht, Wt = y.shape[-2:]
	kx = torch.as_tensor(distill.TeacherIndex(w, student_stride, student_scale, offset, teacher_stride), dtype=torch.float32, device=y.device)
	ky = torch.as_tensor(distill.TeacherIndex(h, student_stride, student_scale, offset, teacher_stride), dtype=torch.float32, device=y.device)
	gx, gy = 2 * kx / (Wt - 1) - 1, 2 * ky / (Ht - 1) - 1
	grid = torch.stack(torch.meshgrid(gy, gx, indexing="ij")[::-1], dim=-1)[None].expand(y.shape[0], -1, -1, -1)
	return F.grid_sample(y, grid, mode="bilinear", align_corners=True, padding_mode="zeros")


class Teacher:
	"""The frozen teacher and its geometry against one student."""

	def __init__(self, run, student_nodes, student_crop, student_scale, student_sigma, student_stride):
		cfg = LoadConfig(run)
		head = cfg.model_config.head_configs.get("centered_instance")
		if head is None:
			raise distill.DistillError(run + " is not a centered-instance model")
		pre = cfg.data_config.preprocessing
		if float(pre.scale) != 1.0:
			raise distill.DistillError("the teacher must crop at full resolution (its input scale is {})".format(pre.scale))
		cfg.model_config.pretrained_backbone_weights = None
		cfg.model_config.pretrained_head_weights = None
		self.model = lm.LightningModel.get_lightning_model_from_config(cfg)
		state = torch.load(os.path.join(run, "best.ckpt"), map_location="cpu", weights_only=False)["state_dict"]
		missing, _ = self.model.load_state_dict(state, strict=False)
		missing = [k for k in missing if not k.endswith("mask_token")]  # DINOv2's unused mask token
		if missing:
			raise distill.DistillError("the teacher's checkpoint lacks {} weights, e.g. {}".format(len(missing), missing[0]))
		self.model.eval().requires_grad_(False)
		self.index = distill.NodeIndex(list(student_nodes), list(head.confmaps.part_names))
		self.rgb = bool(pre.ensure_rgb)
		self.crop = TeacherCrop() or int(pre.crop_size)
		backbone = [v for v in cfg.model_config.backbone_config.values() if v is not None][0]
		self.max_stride = int(backbone["max_stride"])
		self.stride = int(head.confmaps.output_stride)
		self.student = (float(student_stride), float(student_scale))
		self.offset = (self.crop - student_crop) / 2.0
		self.blur = distill.BlurSigma(float(student_sigma), float(student_scale), float(head.confmaps.sigma), self.stride)
		self.peak_floor = float(os.environ.get(distill.ENV_FLOOR, "0"))
		self.device = None

	@torch.no_grad()
	def Targets(self, images, out_hw):
		"""images (B, 1|3, T, T), the dataset's teacher crops -> (B, N, h, w)."""
		if self.device != images.device:
			self.model.to(images.device)
			self.device = images.device
		x = images.expand(-1, 3, -1, -1) if self.rgb and images.shape[1] == 1 else images
		x = normalize_on_gpu(apply_pad_to_stride(x, self.max_stride))
		with torch.autocast(x.device.type, dtype=torch.float16, enabled=x.device.type == "cuda"):
			y = self.model.model(x)["CenteredInstanceConfmapsHead"]
		return ToStudentGrid(y[:, self.index], out_hw, self.student[0], self.student[1], self.offset, self.stride,
			self.blur, self.peak_floor)


_TEACHER = {}


class DistillInstanceModule(_Module):
	"""sleap-nn's centered-instance training step plus the teacher's term;
	validation (and so best.ckpt) unchanged."""

	def training_step(self, batch, batch_idx):
		alpha = float(os.environ.get(distill.ENV_ALPHA, "0"))
		X = normalize_on_gpu(torch.squeeze(batch["instance_image"], dim=1))
		y = torch.squeeze(batch["confidence_maps"], dim=1)
		pred = self.model(X)["CenteredInstanceConfmapsHead"]
		loss = label = F.mse_loss(pred, y)
		self._log_confmap_fg_bg_loss(pred, y, stage="train")
		if alpha > 0:
			target = _TEACHER["t"].Targets(torch.squeeze(batch["teacher_image"], dim=1).to(pred.device), pred.shape[-2:])
			teacher = F.mse_loss(pred, target)
			loss = (1 - alpha) * label + alpha * teacher
			self.log("train/teacher_loss", teacher, on_step=False, on_epoch=True, sync_dist=True)
			self.log("train/label_loss", label, on_step=False, on_epoch=True, sync_dist=True)
		self.log("loss", loss, prog_bar=True, on_step=True, on_epoch=False, sync_dist=True)
		self._accumulate_loss(loss)
		return loss


def Install(config_path):
	"""Load the teacher for this student config and swap the classes in."""
	import sleap_io as sio
	cfg = OmegaConf.load(config_path)
	head = cfg.model_config.head_configs.get("centered_instance")
	if head is None:
		raise distill.DistillError(config_path + " is not a centered-instance config")
	nodes = head.confmaps.part_names
	if not nodes:
		first = list(cfg.data_config.train_labels_path)[0]
		nodes = [n.name for n in sio.load_slp(first, open_videos=False).skeletons[0].nodes]
	if float(os.environ.get(distill.ENV_ALPHA, "0")) > 0:
		pre = cfg.data_config.preprocessing
		_TEACHER["t"] = Teacher(os.environ[distill.ENV_TEACHER], list(nodes), int(pre.crop_size), float(pre.scale),
			float(head.confmaps.sigma), int(head.confmaps.output_stride))
		t = _TEACHER["t"]
		print("campy-distill-train: teacher {} ({} px crops, {} of its body parts), alpha {}, peak floor {}, "
			"blur {:.2f} teacher px".format(os.environ[distill.ENV_TEACHER], t.crop, len(t.index),
			os.environ[distill.ENV_ALPHA], t.peak_floor, t.blur), flush=True)
	cd.CenteredInstanceDataset = DistillInstanceDataset
	lm.TopDownCenteredInstanceLightningModule = DistillInstanceModule
