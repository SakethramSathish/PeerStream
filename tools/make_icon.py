import sys
from PIL import Image

def main():
    if len(sys.argv) < 3:
        print("Usage: python make_icon.py <input_png> <output_ico>")
        sys.exit(1)
        
    input_path = sys.argv[1]
    output_path = sys.argv[2]
    
    img = Image.open(input_path)
    
    # Ensure image is square
    width, height = img.size
    if width != height:
        size = max(width, height)
        new_img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        new_img.paste(img, ((size - width) // 2, (size - height) // 2))
        img = new_img
        
    icon_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(output_path, format='ICO', sizes=icon_sizes)
    print(f"Saved {output_path}")

if __name__ == "__main__":
    main()
