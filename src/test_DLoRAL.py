import os
import shutil
import argparse
import time
import logging

import numpy as np
from PIL import Image
import torch
from torchvision import transforms
import torchvision.transforms.functional as F
import sys

sys.path.append(os.getcwd())
from src.DLoRAL_model import Generator_eval
from src.my_utils.wavelet_color_fix import adain_color_fix, wavelet_color_fix
import PIL.Image
import math
PIL.Image.MAX_IMAGE_PIXELS = 933120000

import glob
import torch
import gc
import cv2
from ram.models.ram_lora import ram
from ram import inference_ram as inference

tensor_transforms = transforms.Compose([
    transforms.ToTensor(),
])

ram_transforms = transforms.Compose([
    transforms.Resize((384, 384)),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
])

center_crop = transforms.CenterCrop(128)
center_crop_gt = transforms.CenterCrop(512)


# init logger
logger = logging.getLogger('DLoRAL')
logger.setLevel(logging.INFO)
timestamp = time.time()
file_handler = logging.FileHandler(".\\log\\inference.{}.log".format(timestamp))
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter("[%(levelname)s][%(asctime)s][%(name)s] %(message)s")
file_handler.setFormatter(formatter)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)
logger.addHandler(file_handler)
logger.addHandler(console_handler)


def get_validation_prompt(args, image, model, device='cuda'):
    validation_prompt = ""
    lq = tensor_transforms(image).unsqueeze(0).to(device)
    lq = ram_transforms(lq).to(dtype=weight_dtype)
    captions = inference(lq, model)
    validation_prompt = f"{captions[0]}, {args.prompt},"

    return validation_prompt


def get_input_files(input_path):
    result = list()
    if os.path.isdir(input_path):
        for file in os.listdir(input_path):
            full_path = os.path.join(input_path, file)
            if not os.path.isdir(full_path):
                result.append(full_path)
    else:
        result.append(input_path)
    return result


def extract_frames_cv2(video_path):
    video_capture = cv2.VideoCapture(video_path)

    frame_number = 0
    success, frame = video_capture.read()
    frame_images = []

    # Loop through frames
    while success:
        # Save each frame as an image
        frame_dir = '{}'.format(video_path.split('.mp4')[0])
        if not os.path.exists(frame_dir):
            os.makedirs(frame_dir)
        frame_filename = "frame_{:04d}.png".format(frame_number)
        cv2.imwrite('{}/{}'.format(frame_dir, frame_filename), frame)
        print("Writing frame to {}/{}".format(frame_dir, frame_filename))

        frame_images.append(os.path.join(frame_dir, frame_filename))

        # Move to the next frame
        success, frame = video_capture.read()
        frame_number += 1

    video_capture.release()
    print(f"Frames extracted from {video_path} successfully!")

    return frame_images


def extract_frames(video_path, ffmpeg_path=None):
    if (ffmpeg_path == None):
        return extract_frames_cv2(video_path)

    video_name = os.path.basename(video_path).split('.')[0]
    logger.info("Start process video: {}".format(video_name))
    # call ffmpeg to extract image from video

    # clean temp dir
    frame_dir = ".\\cache\\raw_frame_dir"
    if os.path.exists(frame_dir):
        shutil.rmtree(frame_dir)
    os.makedirs(frame_dir)

    # get fps
    ffprobe_exe = os.path.join(ffmpeg_path, 'ffprobe')
    ffprobe_paras = ['-v error -select_streams v:0 -show_entries stream=avg_frame_rate',
                     '-of default=noprint_wrappers=1:nokey=1']
    cmd = '{} {} {}'.format(ffprobe_exe, ' '.join(ffprobe_paras), video_path)
    fps = os.popen(cmd).read().strip()
    logger.info("Video FPS: {}".format(fps))

    ffmpeg_exe = os.path.join(ffmpeg_path, 'ffmpeg')
    cmd = f'{ffmpeg_exe} -i {video_path} {frame_dir}\\frame_%05d.png'
    logger.info("Excecute ffmpeg command: {}".format(cmd))
    result = os.system(cmd)
    result = 0
    logger.info(f"result: {result}")

    # read frame names from output folder
    files = os.listdir(frame_dir)
    files.sort()
    files = [ os.path.join(frame_dir, file) for file in files ]
    logger.info("number of frame: {}".format(len(files)))
    logger.info("first file: {}".format(files[0]))
    return (video_name, files, fps)


def merge_frames(video_name, image_dir, output_path, fps, ffmpeg_path):
    if (not ffmpeg_path):
        logger.error("ffmpeg path is Null!")
        return
    if not os.path.exists(output_path):
        os.makedirs(output_path)

    ffmpeg_exe = os.path.join(ffmpeg_path, 'ffmpeg')
    cmd = "{} -r {} -f image2 -i {}\\{} {}".format(ffmpeg_exe, fps,
                                               image_dir, 'frame_%05d.png',
                                               os.path.join(output_path, video_name+'.mp4'))
    logger.info("Execute: {}".format(cmd))
    os.system(cmd)
    return


def compute_frame_difference_mask(frames):
    ambi_matrix = frames.var(dim=0)
    threshold = ambi_matrix.mean().item()
    mask_id = torch.where(ambi_matrix >= threshold, ambi_matrix, torch.zeros_like(ambi_matrix))
    frame_mask = torch.where(mask_id == 0, mask_id, torch.ones_like(mask_id))
    return frame_mask

def pil_center_crop(image, target_size):
    """
    Perform center cropping on a PIL Image.
    Args:
        image: PIL Image object
        target_size: Target dimensions (width, height)
    """
    width, height = image.size
    target_width, target_height = target_size

   # Calculate the top-left coordinates
    left = (width - target_width) // 2
    upper = (height - target_height) // 2

    # Calculate the top-left coordinates
    right = left + target_width
    lower = upper + target_height

    # Perform cropping
    return image.crop((left, upper, right, lower))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--input_path', '-i', type=str, default=None, help='path to the input image')
    parser.add_argument('--output_dir', '-o', type=str, default=None, help='the directory to save the output')
    parser.add_argument('--pretrained_path', type=str, default=None, help='path to a model state dict to be used')
    parser.add_argument('--seed', type=int, default=42, help='Random seed to be used')
    parser.add_argument("--process_size", type=int, default=512)
    parser.add_argument("--upscale", type=int, default=4)
    parser.add_argument("--align_method", type=str, choices=['wavelet', 'adain', 'nofix'], default='adain')
    parser.add_argument("--pretrained_model_path", type=str, default='preset_models/stable-diffusion-2-1-base')
    parser.add_argument('--prompt', type=str, default='', help='user prompts')
    parser.add_argument('--ram_path', type=str, default=None)
    parser.add_argument('--ram_ft_path', type=str, default=None)
    parser.add_argument('--bert_path', type=str, default=None, help='preset bert model path')
    parser.add_argument('--save_prompts', type=bool, default=True)
    # tile setting
    parser.add_argument("--vae_decoder_tiled_size", type=int, default=224)
    parser.add_argument("--vae_encoder_tiled_size", type=int, default=1024)
    parser.add_argument("--latent_tiled_size", type=int, default=96)
    parser.add_argument("--latent_tiled_overlap", type=int, default=32)
    # precision setting
    parser.add_argument("--mixed_precision", type=str, default="fp16")
    # merge lora
    parser.add_argument("--merge_and_unload_lora", default=False)
    # stages
    parser.add_argument("--stages", type=int, default=None)
    parser.add_argument("--load_cfr", action="store_true", )

    # extern ffmpeg path
    parser.add_argument("--ffmpeg_path", type=str, default=None, 
                        help='extern ffmpeg file path')

    args = parser.parse_args()

    # initialize the model
    model = Generator_eval(args)
    model.set_eval()

    #if os.path.isdir(args.input_image):
    #    all_video_data = process_video_directory(args.input_image, args.ffmpeg_path)
    #else:
        # Handle single video case (if input is a single video file)
    #    all_video_data = [extract_frames(args.input_image, args.ffmpeg_path)]

    # get ram model
    DAPE = ram(pretrained=args.ram_path,
               pretrained_condition=args.ram_ft_path,
               bert_path=args.bert_path,
               image_size=384,
               vit='swin_l')
    DAPE.eval()
    DAPE.to("cuda")

    # weight type
    weight_dtype = torch.float32
    if args.mixed_precision == "fp16":
        weight_dtype = torch.float16
    elif args.mixed_precision == "bf16":
        weight_dtype = torch.bfloat16

    # set weight type
    DAPE = DAPE.to(dtype=weight_dtype)
    model.vae = model.vae.to(dtype=weight_dtype)
    model.unet = model.unet.to(dtype=weight_dtype)
    model.cfr_main_net = model.cfr_main_net.to(dtype=weight_dtype)

    if args.stages == 0:
        model.unet.set_adapter(['default_encoder_consistency', 'default_decoder_consistency', 'default_others_consistency'])
    else:
        model.unet.set_adapter(['default_encoder_quality', 'default_decoder_quality',
                                'default_others_quality',
                                'default_encoder_consistency', 'default_decoder_consistency',
                                'default_others_consistency'])
    if args.save_prompts:
        txt_path = os.path.join(args.output_dir, 'txt')
        os.makedirs(txt_path, exist_ok=True)

    # make the output dir
    os.makedirs(args.output_dir, exist_ok=True)
    #print(f"There are {len(all_video_data)} videos to process.")
    frame_num = 2
    frame_overlap = 1

    input_video_list = get_input_files(args.input_path)

    for input_video_path in input_video_list:
#        input_video_path = f'.\\input\\output_{i:03}.mp4'
        logger.info(f"Processing video: {input_video_path}")

        logger.info("Extracting frames.")
        video_name, video_frame_images, fps = extract_frames(input_video_path, args.ffmpeg_path)

        # Initialize a flag to check if the prompt already exists
        exist_prompt = 0

        # Define the save path for the processed video
        image_save_path = os.path.join("cache\\sr_frame_dir\\", video_name)
        if not os.path.exists(image_save_path):
            os.makedirs(image_save_path)

        # Initialize batches for storing input images and their grayscale versions
        input_image_batch = []
        input_image_gray_batch = []
        bname_batch = []
        for image_name in video_frame_images:
            print(image_name)
            # make sure that the input image is a multiple of 8
            input_image = Image.open(image_name).convert('RGB')
            input_image_gray = input_image.convert('L')
            ori_width, ori_height = input_image.size
            rscale = args.upscale
            resize_flag = False

            # If the image is smaller than the required size, scale it up
            if ori_width < args.process_size // rscale or ori_height < args.process_size // rscale:
                scale = (args.process_size // rscale) / min(ori_width, ori_height)
                input_image = input_image.resize((int(scale * ori_width), int(scale * ori_height)))
                input_image_gray = input_image_gray.resize((int(scale * ori_width), int(scale * ori_height)))
                resize_flag = True
            
            # Upscale the image dimensions by the upscale factor
            input_image = input_image.resize((input_image.size[0] * rscale, input_image.size[1] * rscale))
            input_image_gray = input_image_gray.resize((input_image_gray.size[0] * rscale, input_image_gray.size[1] * rscale))

            # Adjust the image dimensions to make sure they are a multiple of 8
            new_width = input_image.width - input_image.width % 8
            new_height = input_image.height - input_image.height % 8
            input_image = input_image.resize((new_width, new_height), Image.LANCZOS)
            input_image_gray = input_image_gray.resize((new_width, new_height), Image.LANCZOS)

            bname = os.path.basename(image_name)
            bname_batch.append(bname)

            # If a prompt does not already exist, generate one
            if exist_prompt == 0:
                # get caption
                validation_prompt = get_validation_prompt(args, input_image, DAPE)
                if args.save_prompts:
                    txt_save_path = f"{txt_path}/{bname.split('.')[0]}.txt"
                    with open(txt_save_path, 'w', encoding='utf-8') as f:
                        f.write(validation_prompt)
                        f.close()
                # print(f"process {image_name}, caption: {validation_prompt}".encode('utf-8'))

                # Set the flag to indicate that the prompt has been generated
                exist_prompt = 1

            print(f"process {image_name}, caption: {validation_prompt}".encode('utf-8'))
            input_image_batch.append(input_image)
            input_image_gray_batch.append(input_image_gray)

        for input_image_index in range(0, len(input_image_batch), (frame_num - frame_overlap)):
            if input_image_index + frame_num - 1 >= len(input_image_batch):
                # Prevent out-of-bound issues for the last few frames
                # For example: if fewer than `frame_num` frames remain, handle this case as needed
                end = len(input_image_batch) - input_image_index
                start = 0
            else:
                start = 0
                end = frame_num

            # Collect the batch of frames to be processed
            input_frames = []
            input_frames_gray = []
            for input_frame_index in range(start, end):
                real_idx = input_image_index + input_frame_index
                # Perform boundary checks to ensure indices are within range
                if real_idx < 0 or real_idx >= len(input_image_batch):
                    continue

                # print(f"input_image_index: {input_image_index}, "
                #       f"input_frame_index: {input_frame_index}, real_idx: {real_idx}")

                current_frame = transforms.functional.to_tensor(input_image_batch[real_idx])
                current_frame_gray = transforms.functional.to_tensor(input_image_gray_batch[real_idx])
                current_frame_gray = torch.nn.functional.interpolate(current_frame_gray.unsqueeze(0), scale_factor=0.125).squeeze(0)
                input_frames.append(current_frame)
                input_frames_gray.append(current_frame_gray)

            input_image_final = torch.stack(input_frames, dim=0)
            input_image_gray_final = torch.stack(input_frames_gray, dim=0)

            uncertainty_map = []
            if input_image_final.shape[0] == 1:
                break
            for image_index in range(input_image_final.shape[0]):
                if image_index != 0:
                    cur_img = input_image_gray_final[image_index]
                    prev_img = input_image_gray_final[image_index - 1]

                    compute_frame = torch.stack([cur_img, prev_img])
                    uncertainty_map_each = compute_frame_difference_mask(input_image_gray_final)
                    uncertainty_map.append(uncertainty_map_each)



            uncertainty_map = torch.stack(uncertainty_map)

            # Model input [b=1, t, c, h, w]
            with torch.no_grad():
                # Normalize input image tensor to range [-1, 1]
                c_t = input_image_final.unsqueeze(0).cuda() * 2 - 1
                c_t = c_t.to(dtype=weight_dtype)
                output_image, _, _, _, _ = model(stages=args.stages, c_t=c_t, uncertainty_map=uncertainty_map.unsqueeze(0).cuda(), prompt=validation_prompt, weight_dtype=weight_dtype)

            frame_t = output_image[0]  # shape: [c, h, w]
            frame_t = (frame_t.cpu() * 0.5 + 0.5)  # Convert the frame back to range [0, 1]
            output_pil = transforms.ToPILImage()(frame_t)


            # Find the index of the corresponding original image (start + output_index)
            src_idx = input_image_index + start + 1
            # Perform boundary check to ensure index is within valid range
            if src_idx < 0 or src_idx >= len(input_image_batch):
                src_idx = max(0, min(src_idx, len(input_image_batch) - 1))

            # Use the corresponding frame for color/band correction
            source_pil = input_image_batch[src_idx]

            if args.align_method == 'adain':
                output_pil = adain_color_fix(target=output_pil, source=source_pil)
            elif args.align_method == 'wavelet':
                output_pil = wavelet_color_fix(target=output_pil, source=source_pil)
            else:
                pass
            
            # If the image was resized earlier, resize it back to its original dimensions
            if resize_flag:
                new_w = int(args.upscale * ori_width)
                new_h = int(args.upscale * ori_height)
                output_pil = output_pil.resize((new_w, new_h), Image.BICUBIC)

            global_frame_counter = src_idx
            out_name = f"frame_{global_frame_counter:05d}.png"
            out_path = f"{image_save_path}/{out_name}"


            output_pil.save(out_path)
            logger.info(f"Saving frame {global_frame_counter} to {out_path}")

            gc.collect()
            torch.cuda.empty_cache()

            # use file as pause signal
            while(os.path.exists("./pause")):
                time.sleep(1)

        logger.info("Merging frames.")
        merge_frames(video_name, image_save_path, args.output_dir, fps, args.ffmpeg_path)
        # clean cache, prevent disk full
        shutil.rmtree(image_save_path)

