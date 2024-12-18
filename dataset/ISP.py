import numpy as np
import cv2
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
import torch.distributions as tdist

import os
os.environ['KMP_DUPLICATE_LIB_OK'] = 'TRUE'




def random_noise_levels():
  """Generates random noise levels from a log-log linear distribution."""
  log_min_shot_noise = np.log(0.0001)
  log_max_shot_noise = np.log(0.012)
  log_shot_noise     = torch.FloatTensor(1).uniform_(log_min_shot_noise, log_max_shot_noise)
  shot_noise = torch.exp(log_shot_noise)

  line = lambda x: 2.18 * x + 1.20
  n    = tdist.Normal(loc=torch.tensor([0.0]), scale=torch.tensor([0.26])) 
  log_read_noise = line(log_shot_noise) + n.sample()
  read_noise     = torch.exp(log_read_noise)
  return shot_noise, read_noise

def add_noise(image, shot_noise=0.01, read_noise=0.0005):
  """Adds random shot (proportional to image) and read (independent) noise."""
  image    = image.permute(1, 2, 0) # Permute the image tensor to HxWxC format from CxHxW format
  variance = image * shot_noise + read_noise
  n        = tdist.Normal(loc=torch.zeros_like(variance), scale=torch.sqrt(variance)) 
  noise    = n.sample()
  out      = image + noise
  out = torch.clamp(out, 0.0, 1.0)  # Clamp to [0, 1] range
  out      = out.permute(2, 0, 1) # Re-Permute the tensor back to CxHxW format
  return out


def shot_and_read_noise(image):
   shot_noise, read_noise = random_noise_levels()
   noisy_img = add_noise(image, shot_noise, read_noise)
   return noisy_img
   
def mosaic_to_rgb(mosaic_image):
    """
    Converts a 4-channel Bayer mosaic back to a 3-channel RGB image 
    while keeping the degradation effect.
    """
    # Assume input mosaic_image is in shape (4, H, W)
    red = mosaic_image[0]        # Red channel
    green_red = mosaic_image[1]  # Green channel (red rows)
    green_blue = mosaic_image[2] # Green channel (blue rows)
    blue = mosaic_image[3]       # Blue channel

    # Initialize an empty 3-channel RGB image
    H, W = red.shape
    rgb_image = torch.zeros((3, H * 2, W * 2), dtype=mosaic_image.dtype, device=mosaic_image.device)

    # Map the Bayer channels back to RGB locations
    rgb_image[0, 0::2, 0::2] = red           # Red in even rows, even cols
    rgb_image[1, 0::2, 1::2] = green_red     # Green (red rows) in even rows, odd cols
    rgb_image[1, 1::2, 0::2] = green_blue    # Green (blue rows) in odd rows, even cols
    rgb_image[2, 1::2, 1::2] = blue          # Blue in odd rows, odd cols

    # Interpolate missing values
    rgb_image = torch.nn.functional.interpolate(rgb_image.unsqueeze(0), scale_factor=0.5, mode='bilinear', align_corners=False)
    return rgb_image.squeeze(0)

def mosaic_to_rgb(mosaic_image):
    """
    Converts a 4-channel Bayer mosaic back to a 3-channel RGB image 
    while keeping the degradation effect.
    """
    # Assume input mosaic_image is in shape (4, H, W)
    red = mosaic_image[0]        # Red channel
    green_red = mosaic_image[1]  # Green channel (red rows)
    green_blue = mosaic_image[2] # Green channel (blue rows)
    blue = mosaic_image[3]       # Blue channel

    # Initialize an empty 3-channel RGB image
    H, W = red.shape
    rgb_image = torch.zeros((3, H * 2, W * 2), dtype=mosaic_image.dtype, device=mosaic_image.device)

    # Map the Bayer channels back to RGB locations
    rgb_image[0, 0::2, 0::2] = red           # Red in even rows, even cols
    rgb_image[1, 0::2, 1::2] = green_red     # Green (red rows) in even rows, odd cols
    rgb_image[1, 1::2, 0::2] = green_blue    # Green (blue rows) in odd rows, even cols
    rgb_image[2, 1::2, 1::2] = blue          # Blue in odd rows, odd cols

    # Interpolate missing values
    rgb_image = torch.nn.functional.interpolate(rgb_image.unsqueeze(0), scale_factor=0.5, mode='bilinear', align_corners=False)
    return rgb_image.squeeze(0)


def mosaic(image):
  """Extracts RGGB Bayer planes from an RGB image."""
  image = image.permute(1, 2, 0) # Permute the image tensor to HxWxC format from CxHxW format
  shape = image.size()
  red   = image[0::2, 0::2, 0]
  green_red  = image[0::2, 1::2, 1]
  green_blue = image[1::2, 0::2, 1]
  blue = image[1::2, 1::2, 2]
  out  = torch.stack((red, green_red, green_blue, blue), dim=-1)
  out  = torch.reshape(out, (shape[0] // 2, shape[1] // 2, 4))
  out  = out.permute(2, 0, 1) # Re-Permute the tensor back to CxHxW format
  out  = mosaic_to_rgb(out) #  4xHxW to 3xHxW
  return out


def random_gains():
  """Generates random gains for brightening and white balance."""
  # RGB gain represents brightening.
  n        = tdist.Normal(loc=torch.tensor([0.8]), scale=torch.tensor([0.1])) 
  rgb_gain = 1.0 / n.sample()

  # Red and blue gains represent white balance.
  red_gain  =  torch.FloatTensor(1).uniform_(1.9, 2.4)
  blue_gain =  torch.FloatTensor(1).uniform_(1.5, 1.9)
  return rgb_gain, red_gain, blue_gain


def safe_invert_gains(image, rgb_gain, red_gain, blue_gain):
  """Inverts gains while safely handling saturated pixels."""
  image = image.permute(1, 2, 0) # Permute the image tensor to HxWxC format from CxHxW format
  gains = torch.stack((1.0 / red_gain, torch.tensor([1.0]), 1.0 / blue_gain)) / rgb_gain
  gains = gains.squeeze()
  gains = gains[None, None, :]
  # Prevents dimming of saturated pixels by smoothly masking gains near white.
  gray  = torch.mean(image, dim=-1, keepdim=True)
  inflection = 0.9
  mask  = (torch.clamp(gray - inflection, min=0.0) / (1.0 - inflection)) ** 2.0
  safe_gains = torch.max(mask + (1.0 - mask) * gains, gains)
  out   = image * safe_gains
  out   = out.permute(2, 0, 1) # Re-Permute the tensor back to CxHxW format
  return out

def white_balance(image):
    rgb_gain, red_gain, blue_gain = random_gains()
    image = safe_invert_gains(image, rgb_gain, red_gain, blue_gain)

    return image


def random_ccm():
  """Generates random RGB -> Camera color correction matrices."""
  # Takes a random convex combination of XYZ -> Camera CCMs.
  xyz2cams = [[[1.0234, -0.2969, -0.2266],
               [-0.5625, 1.6328, -0.0469],
               [-0.0703, 0.2188, 0.6406]],
              [[0.4913, -0.0541, -0.0202],
               [-0.613, 1.3513, 0.2906],
               [-0.1564, 0.2151, 0.7183]],
              [[0.838, -0.263, -0.0639],
               [-0.2887, 1.0725, 0.2496],
               [-0.0627, 0.1427, 0.5438]],
              [[0.6596, -0.2079, -0.0562],
               [-0.4782, 1.3016, 0.1933],
               [-0.097, 0.1581, 0.5181]]]
  num_ccms = len(xyz2cams)
  xyz2cams = torch.FloatTensor(xyz2cams)
  weights  = torch.FloatTensor(num_ccms, 1, 1).uniform_(1e-8, 1e8)
  weights_sum = torch.sum(weights, dim=0)
  xyz2cam = torch.sum(xyz2cams * weights, dim=0) / weights_sum

  # Multiplies with RGB -> XYZ to get RGB -> Camera CCM.
  rgb2xyz = torch.FloatTensor([[0.4124564, 0.3575761, 0.1804375],
                               [0.2126729, 0.7151522, 0.0721750],
                               [0.0193339, 0.1191920, 0.9503041]])
  rgb2cam = torch.mm(xyz2cam, rgb2xyz)

  # Normalizes each row.
  rgb2cam = rgb2cam / torch.sum(rgb2cam, dim=-1, keepdim=True)
  return rgb2cam

def apply_ccm(image, ccm):
  """Applies a color correction matrix."""
  image = image.permute(1, 2, 0) # Permute the image tensor to HxWxC format from CxHxW format
  shape = image.size()
  image = torch.reshape(image, [-1, 3])
  image = torch.tensordot(image, ccm, dims=[[-1], [-1]])
  out   = torch.reshape(image, shape)
  out   = out.permute(2, 0, 1) # Re-Permute the tensor back to CxHxW format
  return out

def color_correction(image):
   rgb2cam = random_ccm()
   image = apply_ccm(image, rgb2cam)

   return image

def gamma_expansion(image):
  """Converts from gamma to linear space."""
  # Clamps to prevent numerical instability of gradients near zero.
  image = image.permute(1, 2, 0) # Permute the image tensor to HxWxC format from CxHxW format
  out   = torch.clamp(image, min=1e-8) ** 2.2
  out   = out.permute(2, 0, 1) # Re-Permute the tensor back to CxHxW format
  return out

   
def inverse_smoothstep(image):
    """Approximately inverts a global tone mapping curve."""
    image = image.permute(1, 2, 0) # Permute the image tensor to HxWxC format from CxHxW format
    image = torch.clamp(image, min=0.0, max=1.0)
    out   = 0.5 - torch.sin(torch.asin(1.0 - 2.0 * image) / 3.0) 
    out   = out.permute(2, 0, 1) # Re-Permute the tensor back to CxHxW format
    return out

if __name__ == '__main__':
    image_path = './data/test/RealSR/hq/Canon_Test_2_Canon_001_HR.png'  # Replace with your image path
    rgb_image = cv2.imread(image_path)
    rgb_image = cv2.cvtColor(rgb_image, cv2.COLOR_BGR2RGB)
    rgb_image = rgb_image.astype(np.float32) / 255.0

    rgb_tensor = torch.from_numpy(rgb_image).permute(2, 0, 1).to(torch.float32) 

    noisy = mosaic(rgb_tensor)
    print(noisy.shape)

    original_image = rgb_tensor.permute(1, 2, 0).numpy()
    noisy_np = noisy.permute(1, 2, 0).detach().numpy()

    fig, axes = plt.subplots(1, 2, figsize=(12, 6))
    axes[0].imshow(original_image)
    axes[0].set_title('Original RGB Image')
    axes[0].axis('off')

    axes[1].imshow(noisy_np)
    axes[1].set_title('Noisy Image')
    axes[1].axis('off')

    plt.show()
