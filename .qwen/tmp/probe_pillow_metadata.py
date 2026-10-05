"""What Pillow 12.3 actually writes and what a re-encode actually drops.

Run outside Django. This exists because the strip control is only real if a GPS IFD survives
writing and disappears on re-encode, and neither of those is knowable from reading the docs.
"""
import io
from fractions import Fraction

from PIL import Image, ImageOps


def jpeg(with_exif=True, orientation=None):
    image = Image.new("RGB", (40, 24), (190, 30, 30))
    exif = image.getexif()
    if with_exif:
        exif[0x010F] = "MakeCam"            # Make
        exif[0x0110] = "ModelPhone 12"      # Model
        exif[0x0132] = "2026:10:04 07:12:00"  # DateTime
        if orientation:
            exif[0x0112] = orientation       # Orientation
        gps = exif.get_ifd(0x8825)
        gps[0] = b"\x02\x00\x00\x00"         # GPSVersionID
        gps[1] = b"N"                        # GPSLatitudeRef
        gps[2] = (Fraction(32), Fraction(4), Fraction(0))
        gps[3] = b"W"                        # GPSLongitudeRef
        gps[4] = (Fraction(96), Fraction(48), Fraction(0))
    out = io.BytesIO()
    image.save(out, format="JPEG", exif=exif.tobytes() if with_exif else b"")
    return out.getvalue()


raw = jpeg()
print("jpeg bytes:", len(raw), "starts", raw[:3])

read = Image.open(io.BytesIO(raw))
exif = read.getexif()
print("make:", exif.get(0x010F), "| model:", exif.get(0x0110))
print("gps ifd:", dict(exif.get_ifd(0x8825)))

# The proposed control: transpose, re-open, save without any exif/info argument.
Image.MAX_IMAGE_PIXELS = None  # only for the probe; the product path keeps the default guard
read.seek(0)
transposed = ImageOps.exif_transpose(read)
stripped_out = io.BytesIO()
transposed.save(stripped_out, format="JPEG", quality=90)
stripped = stripped_out.getvalue()
after = Image.open(io.BytesIO(stripped))
after_exif = after.getexif()
print("stripped bytes:", len(stripped))
print("stripped make:", after_exif.get(0x010F), "gps:", dict(after_exif.get_ifd(0x8825)))
print("stripped info keys:", sorted(k for k in after.info if k not in ("dpi",)))

# Orientation baked in rather than carried.
rot = jpeg(orientation=6)
rotated = Image.open(io.BytesIO(rot))
print("before transpose:", rotated.size, "orientation tag:", rotated.getexif().get(0x0112))
baked = ImageOps.exif_transpose(rotated)
print("after transpose:", baked.size, "orientation tag:", baked.getexif().get(0x0112))

# PNG with a textual metadata chunk.
png = Image.new("RGBA", (20, 20), (10, 200, 10, 255))
info = png.info
info["Software"] = "GeotagMapper 3.1"
png_bytes = io.BytesIO()
png.save(png_bytes, format="PNG", exif=png.getexif().tobytes(), pnginfo=None)
print("png bytes:", len(png_bytes.getvalue()), "b" in "ok")

# Undecodable-but-magic-correct bytes: the case the existing suite fixtures use.
fake = b"\xff\xd8\xff\xe0" + b"\x00" * 2048 + b"\xff\xd9"
try:
    Image.open(io.BytesIO(fake))
    print("fake jpeg: OPENED (unexpected)")
except Exception as exc:
    print("fake jpeg raises:", type(exc).__name__, str(exc)[:70])
try:
    Image.open(io.BytesIO(fake)).verify()
    print("fake jpeg verify() passed")
except Exception as exc:
    print("fake jpeg verify() raises:", type(exc).__name__, str(exc)[:70])
