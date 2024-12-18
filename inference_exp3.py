from argparse import ArgumentParser, Namespace
import torch
from omegaconf import OmegaConf
from torch.utils.data import DataLoader
from tqdm import tqdm
from einops import rearrange
from torch.nn import functional as F
import numpy as np
from accelerate.utils import set_seed
import os
import csv
import pyiqa



from utils import (pad_to_multiples_of
                   ,instantiate_from_config
                   , load_model_from_checkpoint
                   , load_model_from_url
                   , wavelet_decomposition
                   , wavelet_reconstruction
                   , calculate_noise_levels
                   , normalize)


from Similarity import (psnr
                        ,ssim
                        ,brisque
                        ,musiq
                        ,nima
                        ,niqe
                        ,lpips)

from model.SwinIR import SwinIR
from model.cldm import ControlLDM
from model.gaussian_diffusion import Diffusion
from model.cond_fn import MSEGuidance, WeightedMSEGuidance
from model.sampler import SpacedSampler
from dataset.HybridDataset import HybridDataset


MODELS = {
    ### stage_2 model weights
    "sd_v21": "https://huggingface.co/stabilityai/stable-diffusion-2-1-base/resolve/main/v2-1_512-ema-pruned.ckpt",
    "v1_face": "https://huggingface.co/lxq007/DiffBIR-v2/resolve/main/v1_face.pth",
    "v1_general": "https://huggingface.co/lxq007/DiffBIR-v2/resolve/main/v1_general.pth",
    "v2": "https://huggingface.co/lxq007/DiffBIR-v2/resolve/main/v2.pth"
}

 

def run_stage1(swin_model, image, device):
    # image to tensor
    # image = torch.tensor((image / 255.).clip(0, 1), dtype=torch.float32, device=device)
    pad_image = pad_to_multiples_of(image, multiple=64)
    # run
    output, features = swin_model(pad_image)

    return output, features


def run_stage2(
    clean: torch.Tensor,
    cldm: ControlLDM,
    cond_fn,
    diffusion: Diffusion,
    steps: int,
    strength: float,
    tiled: bool,
    tile_size: int,
    tile_stride: int,
    pos_prompt: str,
    neg_prompt: str,
    cfg_scale: float,
    better_start: float,
    device,
    noise_levels: list
) -> torch.Tensor:
    
    ### preprocess
    bs, _, ori_h, ori_w = clean.shape
    
    
    # pad: ensure that height & width are multiples of 64
    pad_clean = pad_to_multiples_of(clean, multiple=64)
    h, w = pad_clean.shape[2:]
    
    
    # prepare conditon
    if not tiled:
        cond = cldm.prepare_condition(pad_clean, [pos_prompt] * bs)
        uncond = cldm.prepare_condition(pad_clean, [neg_prompt] * bs)
    else:
        cond = cldm.prepare_condition_tiled(pad_clean, [pos_prompt] * bs, tile_size, tile_stride)
        uncond = cldm.prepare_condition_tiled(pad_clean, [neg_prompt] * bs, tile_size, tile_stride)
    if cond_fn:
        cond_fn.load_target(pad_clean * 2 - 1)
    old_control_scales = cldm.control_scales
    cldm.control_scales = [strength] * 13
    if better_start:
        # using noised low frequency part of condition as a better start point of 
        # reverse sampling, which can prevent our model from generating noise in 
        # image background.
        _, low_freq = wavelet_decomposition(pad_clean)
        if not tiled:
            x_0 = cldm.vae_encode(low_freq)
        else:
            x_0 = cldm.vae_encode_tiled(low_freq, tile_size, tile_stride)
        
        # x_T = diffusion.q_sample(
        #     x_0,
        #     torch.full((bs, ), diffusion.num_timesteps - 1, dtype=torch.long, device=device),
        #     torch.randn(x_0.shape, dtype=torch.float32, device=device)
        # )

        # add noise
        x_T_s = add_diffusion_noise(x_0 = x_0
                                            , diffusion = diffusion
                                            , noise_levels = noise_levels
                                            , device = device) # [X_T1 , X_T2 ,...]


    else:
        x_T_s = [torch.randn((bs, 4, h // 8, w // 8), dtype=torch.float32, device=device) for _ in range(len(noise_levels))]
    
    ### run sampler
    sampler = SpacedSampler(diffusion.betas)

    clean_samples = []

    for x_T in x_T_s:
        z = sampler.sample(
            model=cldm, device=device, steps=steps, batch_size=bs, x_size=(4, h // 8, w // 8),
            cond=cond, uncond=uncond, cfg_scale=cfg_scale, x_T=x_T, progress=True,
            progress_leave=True, cond_fn=cond_fn, tiled=tiled, tile_size=tile_size, tile_stride=tile_stride
        )
        if not tiled:
            x = cldm.vae_decode(z)
        else:
            x = cldm.vae_decode_tiled(z, tile_size // 8, tile_stride // 8)
        ### postprocess
        cldm.control_scales = old_control_scales
        sample = x[:, :, :ori_h, :ori_w]
        clean_samples.append(sample)
    return clean_samples

# Function to add noise at different levels using diffusion process
def add_diffusion_noise(x_0, diffusion, noise_levels, device):
    noisy_versions = []
    bs = x_0.shape[0]
    for level in noise_levels:
        noise_scale = torch.full((bs,), level, dtype=torch.long, device=device)
        noise = torch.randn(x_0.shape, dtype=torch.float32, device=device)
        x_T = diffusion.q_sample(x_0, noise_scale, noise)
        noisy_versions.append(x_T)
    return noisy_versions

def main(args):
    
    print("[INFO] Start...")

    # config 
    cfg = OmegaConf.load(args.config)
    # device
    device = args.device


    # Initialize Stage 1
    print("[INFO] Load SwinIR...")

    swinir: SwinIR = instantiate_from_config(cfg.model.swinir)
    sd = load_model_from_checkpoint(cfg.test.swin_check_dir)
    swinir.load_state_dict(sd, strict=True)
    swinir.eval().to(device)


    # Initialize Stage 2
    print("[INFO] Load ControlLDM...")
    cldm: ControlLDM = instantiate_from_config(cfg.model.cldm)
    # sd = load_model_from_url(MODELS["sd_v21"])
    sd = load_model_from_checkpoint(cfg.test.diffusion_check_dir)
    unused = cldm.load_pretrained_sd(sd)
    print(f"[INFO] strictly load pretrained sd_v2.1, unused weights: {unused}")

    ### load controlnet
    print("[INFO] Load ControlNet...")
    # control_sd = load_model_from_url(MODELS["v2"])
    control_sd = load_model_from_checkpoint(cfg.test.controlnet_check_dir)
    cldm.load_controlnet_from_ckpt(control_sd)
    print(f"[INFO] strictly load controlnet weight")
    cldm.eval().to(device)

    ### load diffusion
    print("[INFO] Load Diffusion...")
    diffusion: Diffusion = instantiate_from_config(cfg.model.diffusion)
    diffusion.to(device)

    # Initialize Condition
    if not args.guidance:
        cond_fn = None
    else:
        if args.g_loss == "mse":
            cond_fn_cls = MSEGuidance
        elif args.g_loss == "w_mse":
            cond_fn_cls = WeightedMSEGuidance
        else:
            raise ValueError(args.g_loss)
        cond_fn = cond_fn_cls(
            scale=args.g_scale, t_start=args.g_start, t_stop=args.g_stop,
            space=args.g_space, repeat=args.g_repeat
        )


    # Setup data
    print("[INFO] Setup Dataset...")
    dataset: HybridDataset = instantiate_from_config(cfg.dataset)
    test_loader = DataLoader(
    dataset=dataset, batch_size=cfg.test.batch_size,
    num_workers=cfg.test.num_workers,
    shuffle=True)


    # Noising Setup
    noise_levels = calculate_noise_levels(
                    num_timesteps = diffusion.num_timesteps
                    , num_levels = args.num_levels)


    # Setup Similarity Metrics
    psnr_metric = pyiqa.create_metric('psnr')
    ssim_metric = pyiqa.create_metric('ssim')
    lpips_metric = pyiqa.create_metric('lpips')
    brisque_metric = pyiqa.create_metric('brisque')
    nima_metric = pyiqa.create_metric('nima')
    niqe_metric = pyiqa.create_metric('niqe')
    musiq_metric = pyiqa.create_metric('musiq')



    # Setup result path
    result_syn_path = os.path.join(cfg.test.test_result_dir, 'results-3-syn.csv')
    result_real_path = os.path.join(cfg.test.test_result_dir, 'results-3-real.csv')

    header = [
        "Image_Index", "Noise_level", "PSNR", "SSIM", "LPIPS",
        "BRISQUE", "MUSIQ", "NIMA", "NIQE"
    ]

    # Open CSV file in write mode and write the header
    with open(result_real_path, mode='w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(header)  # Write the header

    # Open CSV file in write mode and write the header
    with open(result_syn_path, mode='w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(header)  # Write the header


    for batch_idx, (real, syn) in enumerate(tqdm(test_loader)):

        # Stage 1
        real, syn = real.to(device), syn.to(device)
        # real, syn = real.to(device) / 255.0, syn.to(device) / 255.0
        # real, syn = real.clip(0, 1).float(), syn.clip(0, 1).float()

        
        # real = torch.tensor((real / 255.).clip(0, 1), dtype=torch.float32, device=device)
        # syn = torch.tensor((syn / 255.).clip(0, 1), dtype=torch.float32, device=device)

        # set pipeline output size
        h, w = real.shape[2:]
        final_size = (h, w)

        



        with torch.no_grad():
            real_clean, _ = run_stage1(swin_model=swinir, image=real, device=device)
            syn_clean, _ = run_stage1(swin_model=swinir, image=syn, device=device)


        torch.cuda.empty_cache()

        with torch.no_grad():

            real_samples = run_stage2(
            clean = real_clean,
            cldm = cldm,
            cond_fn = cond_fn,
            diffusion = diffusion,
            steps = args.steps,
            strength = 1.0,
            tiled = args.tiled,
            tile_size = args.tile_size,
            tile_stride = args.tile_stride,
            pos_prompt = args.pos_prompt,
            neg_prompt = args.neg_prompt,
            cfg_scale = args.cfg_scale,
            better_start = args.better_start,
            device = device,
            noise_levels = noise_levels)


            syn_samples = run_stage2(
            clean = syn_clean,
            cldm = cldm,
            cond_fn = cond_fn,
            diffusion = diffusion,
            steps = args.steps,
            strength = 1.0,
            tiled = args.tiled,
            tile_size = args.tile_size,
            tile_stride = args.tile_stride,
            pos_prompt = args.pos_prompt,
            neg_prompt = args.neg_prompt,
            cfg_scale = args.cfg_scale,
            better_start = args.better_start,
            device = device,
            noise_levels = noise_levels)


        torch.cuda.empty_cache()
        similarity_vals = []
        for idx, (real_sample, syn_sample) in enumerate(zip(real_samples, syn_samples)):

            # colorfix (borrowed from StableSR, thanks for their work)
            # real_sample = (real_sample + 1) / 2
            # syn_sample = (syn_sample + 1) / 2

            real_sample = normalize(real_sample)
            syn_sample = normalize(syn_sample)

            real_sample = wavelet_reconstruction(real_sample, real_clean)
            syn_sample = wavelet_reconstruction(syn_sample, syn_clean)


            real_sample = normalize(real_sample)
            syn_sample = normalize(syn_sample)



            n_real_clean = normalize(real_clean)
            n_syn_clean = normalize(syn_clean)
        
            # real_sample = real_sample.contiguous().clamp(0, 1).to(torch.float32)
            # syn_sample = syn_sample.contiguous().clamp(0, 1).to(torch.float32)


            # Image Quality Metrics Calculation
            real_metrics = {
                "psnr": psnr(real_sample, real_clean, psnr_metric),
                "ssim": ssim(real_sample, real_clean, ssim_metric),
                "lpips": lpips(real_sample, real_clean, lpips_metric),
                "brisque": brisque(real_sample, brisque_metric),
                "musiq": musiq(real_sample, musiq_metric),
                "nima": nima(real_sample, nima_metric),
                "niqe": niqe(real_sample, niqe_metric),
            }

            syn_metrics = {
                "psnr": psnr(syn_sample, syn_clean, psnr_metric),
                "ssim": ssim(syn_sample, syn_clean, ssim_metric),
                "lpips": lpips(syn_sample, syn_clean, lpips_metric),
                "brisque": brisque(syn_sample, brisque_metric),
                "musiq": musiq(syn_sample, musiq_metric),
                "nima": nima(syn_sample, nima_metric),
                "niqe": niqe(syn_sample, niqe_metric),
            }
            similarity_vals.append((real_metrics, syn_metrics))

            # # Writing results to CSV
            # for image_type, metrics in zip(["Real", "Syn"], [real_metrics, syn_metrics]):
            #     row = [
            #         image_type, idx,  # Image_Type and Image_Index
            #         metrics["psnr"], metrics["ssim"], metrics["lpips"],
            #         metrics["brisque"], metrics["musiq"], metrics["nima"],
            #         metrics["niqe"]
            #     ]
            #     writer.writerow(row) 

        with open(result_real_path, mode='w', newline='') as real_file, open(result_syn_path, mode='w', newline='') as syn_file:
            real_writer = csv.writer(real_file)
            syn_writer = csv.writer(syn_file)

            for i in range(len(real)):
                for noise_level, (real_metrics, syn_metrics) in zip(['high', 'mid', 'low'] ,similarity_vals):
                    img_idx = batch_idx + i
                    real_row = [img_idx,
                        noise_level,  
                        real_metrics["psnr"][i], real_metrics["ssim"][i], real_metrics["lpips"][i],
                        real_metrics["brisque"][i], real_metrics["musiq"][i], real_metrics["nima"][i],
                        real_metrics["niqe"][i]
                    ]
                    real_writer.writerow(real_row)

                    syn_row = [img_idx,
                        noise_level,  
                        syn_metrics["psnr"][i], syn_metrics["ssim"][i], syn_metrics["lpips"][i],
                        syn_metrics["brisque"][i], syn_metrics["musiq"][i], syn_metrics["nima"][i],
                        syn_metrics["niqe"][i]
                    ]
                    syn_writer.writerow(syn_row)

                

def check_device(device: str) -> str:
    if device == "cuda":
        if not torch.cuda.is_available():
            print("CUDA not available because the current PyTorch install was not "
                  "built with CUDA enabled.")
            device = "cpu"
    else:
        if device == "mps":
            if not torch.backends.mps.is_available():
                if not torch.backends.mps.is_built():
                    print("MPS not available because the current PyTorch install was not "
                          "built with MPS enabled.")
                    device = "cpu"
                else:
                    print("MPS not available because the current MacOS version is not 12.3+ "
                          "and/or you do not have an MPS-enabled device on this machine.")
                    device = "cpu"
    print(f"using device {device}")
    return device



def parse_args() -> Namespace:
    parser = ArgumentParser()
    ### model parameters
    # parser.add_argument("--task", type=str, required=True, choices=["sr", "dn", "fr", "fr_bg"])
    parser.add_argument("--upscale", type=float, required=True)
    # parser.add_argument("--version", type=str, default="v2", choices=["v1", "v2"])
    ### sampling parameters
    parser.add_argument("--steps", type=int, default=50)
    parser.add_argument("--better_start", action="store_true")
    parser.add_argument("--tiled", action="store_true")
    parser.add_argument("--tile_size", type=int, default=512)
    parser.add_argument("--tile_stride", type=int, default=256)
    parser.add_argument("--pos_prompt", type=str, default="")
    parser.add_argument("--neg_prompt", type=str, default="low quality, blurry, low-resolution, noisy, unsharp, weird textures")
    parser.add_argument("--cfg_scale", type=float, default=4.0)
    ### input parameters
    # parser.add_argument("--input", type=str, required=True)
    parser.add_argument("--n_samples", type=int, default=1)
    ### guidance parameters
    parser.add_argument("--guidance", action="store_true")
    parser.add_argument("--g_loss", type=str, default="w_mse", choices=["mse", "w_mse"])
    parser.add_argument("--g_scale", type=float, default=0.0)
    parser.add_argument("--g_start", type=int, default=1001)
    parser.add_argument("--g_stop", type=int, default=-1)
    parser.add_argument("--g_space", type=str, default="latent")
    parser.add_argument("--g_repeat", type=int, default=1)
    ### output parameters
    # parser.add_argument("--output", type=str, required=True)
    ### common parameters
    parser.add_argument("--seed", type=int, default=231)
    parser.add_argument("--device", type=str, default="cuda", choices=["cpu", "cuda", "mps"])
    ### number of noise levels
    parser.add_argument("--num_levels", type=int, default=3)
    ### config
    parser.add_argument("--config", type=str, required=True)
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    args.device = check_device(args.device)
    set_seed(args.seed)
    main(args)
    print("done!")

