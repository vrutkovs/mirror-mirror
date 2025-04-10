from statistics import mode
#!/usr/bin/env python

# base libs
import os
import base64
import io
from typing import Dict, Union

# import libraries
import random
from torch import Generator
from kserve import Model, InferRequest, InferResponse
from kserve.errors import InvalidInput
from diffusers.models.autoencoders.autoencoder_kl import AutoencoderKL
from diffusers.pipelines.stable_diffusion.pipeline_stable_diffusion_depth2img import StableDiffusionDepth2ImgPipeline
from .tools import get_accelerator_device, schedulers, RANDOM_BITS_LENGTH
from PIL import Image
# from diffusers.utils.pil_utils import pt_to_pil

# Torch tweaks
import torch
torch._inductor.config.conv_1x1_as_mm = True
torch._inductor.config.coordinate_descent_tuning = True
torch._inductor.config.epilogue_fusion = False
torch._inductor.config.coordinate_descent_check_all_directions = True
torch.set_float32_matmul_precision('high')
torch.backends.cuda.matmul.allow_tf32 = True

# stable diffusion class
# instantiate this to perform image generation
class DiffusersModel(Model):
    # initialize class
    def __init__(self, name: str, vae_model: str | None = None, lora_model: str | None = None, lora_weight_name: str | None = None):
        super().__init__(name)
        self.model_id = os.environ.get("MODEL_ID", default="/mnt/models")
        # stable diffusion pipeline
        self.pipeline = None
        # health check
        self.ready = False
        # accelerator device
        self.device = None
        # vae
        self.vae_model = os.environ.get("VAE_MODEL", None)
        # lora
        self.lora_model = os.environ.get("LORA_MODEL", None)
        self.lora_weight_name = os.environ.get("LORA_WEIGHT_NAME", None)

        print(f"VAE: {self.vae_model}")
        print(f"Lora model: {self.lora_model}")
        print(f"Lora weight name: {self.lora_weight_name}")

        # load model
        self.load()

    # load weights and instantiate pipeline
    def load(self):
        print(f"Loading model {self.model_id}")
        # detect accelerator
        device, dtype = get_accelerator_device()

        pipeline_args = {
        }
        if self.vae_model:
            print(f"Loading VAE {self.vae_model}")
            vae = AutoencoderKL.from_pretrained(self.vae_model, torch_dtype=dtype)
            pipeline_args["vae"] = vae

        try:
            pipeline = StableDiffusionDepth2ImgPipeline.from_pretrained(self.model_id, **pipeline_args)
        except Exception:
            # try loading from a single file..
            pipeline = StableDiffusionDepth2ImgPipeline.from_pretrained(self.model_id, **pipeline_args, torch_dtype=dtype, variant="fp16", use_safetensors=True, device_map="balanced")

        lora_noise_loaded = False

        if self.model_id == "stabilityai/stable-diffusion-xl-base-1.0":
            print("Loading Stable Diffusion XL offset noise LoRA")
            lora_noise_loaded = True
            pipeline.load_lora_weights(self.model_id, weight_name="sd_xl_offset_example-lora_1.0.safetensors", adapter_name="offset_noise")

        if self.lora_model:
            print(f"Loading LoRA {self.lora_model} ({self.lora_weight_name})")
            pipeline.load_lora_weights(self.lora_model, weight_name=self.lora_weight_name, adapter_name="custom_lora")
            if lora_noise_loaded:
                pipeline.set_adapters(["offset_noise", "custom_lora"], adapter_weights=[0.7, 0.8])

        pipeline.enable_attention_slicing()
        pipeline.unet.to(memory_format=torch.channels_last)
        if "vae" in pipeline.components:
            pipeline.vae.to(memory_format=torch.channels_last)
        pipeline.fuse_qkv_projections()
        pipeline.to(device)
        # pipeline.enable_model_cpu_offload()

        # FreeU
        # pipeline.enable_freeu(s1=0.9, s2=0.2, b1=1.3, b2=1.4)

        # Max autotune
        # pipeline.unet = torch.compile(pipeline.unet, mode="max-autotune", fullgraph=True)
        # pipeline.vae.decode = torch.compile(pipeline.vae.decode, mode="max-autotune", fullgraph=True)

        # Reduce overhead
        # pipeline.unet = torch.compile(pipeline.unet, mode="reduce-overhead", fullgraph=True)
        # pipeline.vae.decode = torch.compile(pipeline.vae.decode, mode="reduce-overhead", fullgraph=True)

        self.pipeline = pipeline
        self.device = device
        # The ready flag is used by model ready endpoint for readiness probes,
        # set to True when model is loaded successfully without exceptions.
        print("Loading complete")
        self.ready = True
        return True

    # process incoming request payload.
    # An example JSON payload:
    #  {
    #    "instances": [
    #      {
    #        "image_b64": "",
    #        "prompt": "a wizard smoking a pipe",
    #        "negative_prompt": "ugly, deformed, bad anatomy",
    #        "num_inference_steps": 20,
    #        "width": 512,
    #        "height": 512,
    #        "guidance_scale": 7,
    #        "seed": 772847624537827,
    #        "scheduler": "DPM++ 2M",
    #      }
    #    ]
    #  }
    # validate input request: v2 payloads not yet supported
    def preprocess(self, payload: Union[Dict, InferRequest], headers: Dict[str, str] = None) -> Dict:
        print("payload:")
        print(payload)
        print("---")

        if isinstance(payload, Dict) and "instances" in payload:
            headers["request-type"] = "v1"
        # KServe InferRequest not yet supported
        elif isinstance(payload, InferRequest):
            raise InvalidInput("v2 protocol not implemented")
        else:
            # malformed or missing input payload
            raise InvalidInput("invalid payload")

        if len(payload["instances"]) == 0:
            raise InvalidInput("invalid payload - empty instances")

        if "image_b64" not in payload["instances"][0]:
            raise InvalidInput("invalid payload - image_b64 not set")

        # return generation data
        return payload["instances"][0]

    # perform a forward pass (inference) and return generated data
    def predict(self, payload: Union[Dict, InferRequest], headers: Dict[str, str] = None, response_headers: Dict[str, str] = None,) -> Union[Dict, InferResponse]:
        # generate images
        # set a fixed seed if necessary
        if payload.get("seed", -1) == -1:
            payload["generator"] = Generator(self.device).manual_seed(random.getrandbits(RANDOM_BITS_LENGTH))
        else:
            payload["generator"] = Generator(self.device).manual_seed(payload.get("seed"))

        # Setup Scheduler
        print(f"Generating with Noise Scheduler {payload.get('scheduler')}")
        self.pipeline.scheduler = schedulers.get(payload.get("scheduler")).from_config(self.pipeline.scheduler.config)

        # Convert base64 encoded image to PIL Image
        image_b64 = payload.get("image_b64")
        image = Image.open(io.BytesIO(base64.b64decode(image_b64)))
        payload["image"] = image
        del payload["image_b64"]
        print(f"image: {image_b64}")

        if self.lora_model:
            payload["cross_attention_kwargs"] = {"scale": 0.9}

        # generate image
        print(f"Params: {payload}")
        denoising = 0.8
        #tensor = self.pipeline(**payload, denoising_end=denoising, output_type="latent").images
        # if self.refiner:
        #     tensor = self.refiner(**payload, denoising_start=denoising, image=tensor[0]).images
        # Convert tensor to PIL Image
        # image = pt_to_pil(tensor[0])
        active_adapters = self.pipeline.get_list_adapters()
        print(f"Adapters: {active_adapters}")

        prompt = payload["prompt"]
        del payload["prompt"]
        result = self.pipeline(prompt, **payload).images[0]

        # convert images to PNG and encode in base64
        # for easy sending via response payload
        image_bytes = io.BytesIO()
        result.save(image_bytes, format='PNG')
        image_bytes.seek(0)
        # base64 encoding
        im_b64 = base64.b64encode(image_bytes.read())
        if isinstance(im_b64, bytes):
            im_b64 = im_b64.decode("UTF-8")

        # return payload
        return {
            "predictions": [
                {
                    "model_name": self.model_id,
                    "prompt": prompt,
                    "negative_prompt": payload.get("negative_prompt", ""),
                    "num_inference_steps": payload.get("num_inference_steps"),
                    "width": payload.get("width", "unspecified"),
                    "height": payload.get("height", "unspecified"),
                    "guidance_scale": payload.get("guidance_scale", "unspecified"),
                    "strength": payload.get("strength", "unspecified"),
                    "seed": payload.get("seed", "-1"),
                    "scheduler": payload.get("scheduler", "unspecified"),
                    "image": {
                        "format": "PNG",
                        "b64": im_b64
                    }
                }
            ]}
