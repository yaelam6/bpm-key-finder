"""Creates AudioLens.ico for the Windows build."""
from PIL import Image, ImageDraw

def make_icon(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    margin = max(1, int(size * 0.04))
    d.ellipse([margin, margin, size - margin, size - margin],
              fill=(20, 16, 40, 255))

    ring = max(1, int(size * 0.035))
    d.ellipse([margin, margin, size - margin, size - margin],
              outline=(124, 92, 252, 255), width=ring)

    inner_m = int(size * 0.18)
    inner_ring = max(1, int(size * 0.022))
    d.ellipse([inner_m, inner_m, size - inner_m, size - inner_m],
              outline=(160, 130, 255, 180), width=inner_ring)

    # Draw ⊙ geometrically (reliable on all platforms)
    cx, cy = size / 2, size / 2
    sym_r = size * 0.26
    sym_ring = max(1, int(size * 0.038))
    d.ellipse([cx - sym_r, cy - sym_r, cx + sym_r, cy + sym_r],
              outline=(255, 255, 255, 255), width=sym_ring)
    dot_r = sym_r * 0.28
    d.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r],
              fill=(255, 255, 255, 255))

    return img

sizes = [16, 32, 48, 64, 128, 256]
images = [make_icon(s) for s in sizes]
images[0].save(
    "AudioLens.ico",
    format="ICO",
    sizes=[(s, s) for s in sizes],
    append_images=images[1:],
)
print("AudioLens.ico created successfully")
