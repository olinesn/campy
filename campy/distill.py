"""
campy-distill-train: train a centered-instance model with a larger model as
its teacher (knowledge distillation), on top of sleap-nn's own trainer.

	campy-distill-train student.yaml --teacher runs/big_instance --alpha 0.5
	campy-distill-train student.yaml --teacher runs/big_instance --alpha 0.5 trainer_config.optimizer.lr=5e-5

student.yaml is an ordinary sleap-nn centered-instance config (the light
model you want); anything after it that isn't an option of this command goes
to `sleap-nn train` as a config override. The teacher is a trained
centered-instance model folder (best.ckpt and its training config), any
backbone sleap-nn can load, e.g. a DINOv2 one too slow to run live.

What changes from plain `sleap-nn train`: on every training step the teacher
sees the same augmented crop as the student, and the student learns from both

	loss = (1 - alpha) * MSE(student, label maps) + alpha * MSE(student, teacher maps)

- The teacher's crop. The dataset cuts a second, teacher-sized crop (its
  config's crop_size, at full resolution) from the same augmented image,
  around the same centre, before the student's crop is cut and shrunk. Both
  share the centre, so student-crop pixel x is teacher-crop pixel
  x + (teacher crop - student crop) / 2. The rotation pre-crop is made big
  enough for the teacher's crop; the student's own crop is unchanged.
- Teacher maps on the student's grid. The teacher's spots (its sigma, in
  full-frame pixels) are blurred to the student's spot size (sigma / input
  scale), rescaled per body part so each keeps the teacher's own peak height,
  then sampled bilinearly at the student's output pixels. Body parts are
  matched by name; the teacher may have more.
- --peak-floor (default 0.3): body parts the teacher is sure of (peak at or
  over it) are rescaled to a peak of 1, like a label map; unsure ones keep the
  teacher's low peak. Without it the student copies the teacher's lower peaks
  and leaves more body parts below campy-live's 0.2 threshold.
- Validation and the choice of best.ckpt are unchanged: the label loss on
  the validation frames. The teacher is not part of the student: not saved in
  its checkpoint, not trained, not counted in its parameters.
- --alpha 0 is plain sleap-nn training through the same code (a control).

The teacher runs under fp16 autocast on the training GPU. Training needs
sleap-nn 0.3.5 or later (the dataset hooks this uses) and, for a DINOv2
teacher, transformers. Settings reach the data-loader processes through
environment variables (CAMPY_DISTILL_*), so spawned workers see them too.

Results on the 8-camera box (docs/models.md, "Round 5"): with more
pseudo-labelled frames and alpha 0.5, a 16-filter model became more accurate
than the 24-filter one trained without the teacher, and a 24-filter one
trained with it closed half of its 2D gap to the teacher.
"""

import argparse, math, os, sys

import numpy as np

ENV_TEACHER, ENV_ALPHA, ENV_CROP, ENV_FLOOR = ("CAMPY_DISTILL_TEACHER", "CAMPY_DISTILL_ALPHA",
	"CAMPY_DISTILL_TEACHER_CROP", "CAMPY_DISTILL_PEAK_FLOOR")


class DistillError(Exception):
	pass


def TeacherIndex(n, student_stride, student_scale, offset, teacher_stride):
	"""Where the student's output pixels 0..n-1 (one axis) fall on the
	teacher's output grid, as fractional indices.

	sleap-nn's label coordinates: an input pixel is crop pixel * input scale,
	and output pixel i is input pixel i * output stride. So student output i is
	student-crop pixel i * stride / scale, which is teacher-crop pixel that +
	offset, which is teacher output (that) / teacher stride (teacher at scale 1)."""
	i = np.arange(n, dtype=np.float64)
	return (i * student_stride / student_scale + offset) / teacher_stride


def BlurSigma(student_sigma, student_scale, teacher_sigma, teacher_stride):
	"""The Gaussian blur, in teacher output pixels, that widens the teacher's
	spots (teacher_sigma full-frame px) to the student's (student_sigma input
	px = student_sigma / scale full-frame px). 0 when the teacher's are wider."""
	extra = (student_sigma / student_scale) ** 2 - teacher_sigma ** 2
	return math.sqrt(extra) / teacher_stride if extra > 0 else 0.0


def NodeIndex(student_nodes, teacher_nodes):
	"""The teacher channel of each student body part, by name."""
	missing = [n for n in student_nodes if n not in teacher_nodes]
	if missing:
		raise DistillError("the teacher has no {}: it has {}".format(", ".join(missing), ", ".join(teacher_nodes)))
	return [list(teacher_nodes).index(n) for n in student_nodes]


def ParseArgs(argv):
	p = argparse.ArgumentParser(prog="campy-distill-train", description=__doc__.split("\n\n")[0],
		formatter_class=argparse.RawDescriptionHelpFormatter)
	p.add_argument("config", help="the student's sleap-nn centered-instance config (.yaml)")
	p.add_argument("--teacher", required=True, help="the teacher's model folder (best.ckpt + training config)")
	p.add_argument("--alpha", type=float, default=0.5, help="weight of the teacher's maps, 0..1 (default 0.5; 0 = plain training)")
	p.add_argument("--peak-floor", type=float, default=0.3,
		help="teacher body parts with a peak at or over it are rescaled to peak 1 (default 0.3; 0 = keep the teacher's peaks)")
	p.add_argument("--teacher-crop", type=int, default=0, help="teacher crop in full-frame px (default: its config's crop_size)")
	args, overrides = p.parse_known_args(argv)
	if not 0 <= args.alpha <= 1:
		p.error("--alpha is between 0 and 1")
	return args, overrides


def Main(argv=None):
	args, overrides = ParseArgs(sys.argv[1:] if argv is None else argv)
	os.environ[ENV_TEACHER] = os.path.abspath(args.teacher)
	os.environ[ENV_ALPHA] = repr(args.alpha)
	os.environ[ENV_FLOOR] = repr(args.peak_floor)
	if args.teacher_crop:
		os.environ[ENV_CROP] = str(args.teacher_crop)
	try:
		from campy import distillsleap
		distillsleap.Install(args.config)
	except DistillError as e:
		sys.exit("campy-distill-train: " + str(e))
	from sleap_nn.cli import cli
	sys.argv = ["sleap-nn", "train", args.config] + list(overrides)
	cli()


if __name__ == "__main__":
	Main()
