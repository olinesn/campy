"""
Pull the SY (sync) channel out of a SpikeGLX imec .ap.bin, standard library only (the DoublePoke Ephys PC's Python
has no numpy): writes <name>.imec0.sy.bin (int16, one channel) and its .meta (nSavedChans=1, the sample rate, the
original's fileSHA1 and size). campy-ephyssync --ap <that .sy.bin> then fits the NI clock to the probe's.
  python sy_extract.py <file.imec0.ap.bin> [out folder]
"""
import array, os, sys

def main(ap, out_dir=None):
    meta = {}
    with open(ap[:-4] + ".meta", encoding="utf-8", errors="replace") as f:
        for line in f:
            k, sep, v = line.strip().partition("=")
            if sep:
                meta[k] = v
    n = int(meta["nSavedChans"])
    base = os.path.basename(ap)[:-len(".ap.bin")] + ".sy"
    out = os.path.join(out_dir or os.path.dirname(ap), base + ".bin")
    rows = 32768
    samples = 0
    with open(ap, "rb") as src, open(out, "wb") as dst:
        while True:
            buf = src.read(rows * n * 2)
            if not buf:
                break
            a = array.array("h")
            a.frombytes(buf[:len(buf) // (2 * n) * 2 * n])
            sy = a[n - 1::n]
            sy.tofile(dst)
            samples += len(sy)
    keep = ("imSampRate", "typeThis", "firstSample", "fileCreateTime", "syncSourceIdx", "syncSourcePeriod",
        "imDatPrb_sn", "imDatPrb_pn", "imDatPrb_slot", "imDatPrb_port", "imDatPrb_dock", "imErrFlags_IS_CT_SR_LK_PP_SY")
    with open(out[:-4] + ".meta", "w") as f:
        for k in keep:
            if k in meta:
                f.write("{}={}\n".format(k, meta[k]))
        f.write("nSavedChans=1\nfileSizeBytes={}\nfileTimeSecs={}\nsyFromFile={}\nsyFromFileSHA1={}\nsyFromFileSizeBytes={}\n"
            .format(samples * 2, samples / float(meta["imSampRate"]), os.path.basename(ap), meta.get("fileSHA1", ""),
            meta.get("fileSizeBytes", "")))
    print("{}: {} samples ({:.1f} s) of SY from {} channels".format(out, samples, samples / float(meta["imSampRate"]), n))

if __name__ == "__main__":
    main(*sys.argv[1:])
