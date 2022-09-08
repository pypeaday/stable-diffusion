import json
import base64
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ldm.dream.pngwriter import PngWriter
from threading import Event

class CanceledException(Exception):
    pass

class DreamServer(BaseHTTPRequestHandler):
    model = None
    outdir = None
    canceled = Event()

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            with open("./static/dream_web/index.html", "rb") as content:
                self.wfile.write(content.read())
        elif self.path == "/config.js":
            # unfortunately this import can't be at the top level, since that would cause a circular import
            from ldm.gfpgan.gfpgan_tools import gfpgan_model_exists
            self.send_response(200)
            self.send_header("Content-type", "application/javascript")
            self.end_headers()
            config = {
                'gfpgan_model_exists': gfpgan_model_exists
            }
            self.wfile.write(bytes("let config = " + json.dumps(config) + ";\n", "utf-8"))
        elif self.path == "/run_log.json":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            output = []
            
            log_file = os.path.join(self.outdir, "dream_web_log.txt")
            if os.path.exists(log_file):
                with open(log_file, "r") as log:
                    for line in log:
                        url, config = line.split(": {", maxsplit=1)
                        config = json.loads("{" + config)
                        config["url"] = url.lstrip(".")
                        if os.path.exists(url):
                            output.append(config)

            self.wfile.write(bytes(json.dumps({"run_log": output}), "utf-8"))
        elif self.path == "/cancel":
            self.canceled.set()
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(bytes('{}', 'utf8'))
        else:
            path = "." + self.path
            cwd = os.path.realpath(os.getcwd())
            is_in_cwd = os.path.commonprefix((os.path.realpath(path), cwd)) == cwd
            if not (is_in_cwd and os.path.exists(path)):
                self.send_response(404)
                return
            mime_type = mimetypes.guess_type(path)[0]
            if mime_type is not None:
                self.send_response(200)
                self.send_header("Content-type", mime_type)
                self.end_headers()
                with open("." + self.path, "rb") as content:
                    self.wfile.write(content.read())
            else:
                self.send_response(404)

    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()

        # unfortunately this import can't be at the top level, since that would cause a circular import
        from ldm.gfpgan.gfpgan_tools import gfpgan_model_exists

        content_length = int(self.headers['Content-Length'])
        post_data = json.loads(self.rfile.read(content_length))
        prompt = post_data['prompt']
        initimg = post_data['initimg']
        strength = float(post_data['strength'])
        iterations = int(post_data['iterations'])
        steps = int(post_data['steps'])
        width = int(post_data['width'])
        height = int(post_data['height'])
        fit      = 'fit' in post_data
        seamless = 'seamless' in post_data
        cfgscale = float(post_data['cfgscale'])
        sampler_name  = post_data['sampler']
        variation_amount = float(post_data['variation_amount'])
        with_variations = post_data['with_variations']
        gfpgan_strength = float(post_data['gfpgan_strength']) if gfpgan_model_exists else 0
        upscale_level    = post_data['upscale_level']
        upscale_strength = post_data['upscale_strength']
        upscale = [int(upscale_level),float(upscale_strength)] if upscale_level != '' else None
        progress_images = 'progress_images' in post_data
        seed = self.model.seed if int(post_data['seed']) == -1 else int(post_data['seed'])

        if with_variations != '':
            parts = []
            broken = False
            for part in with_variations.split(','):
                seed_and_weight = part.split(':')
                if len(seed_and_weight) != 2:
                    print(f'could not parse with_variation part "{part}"')
                    broken = True
                    break
                try:
                    vseed = int(seed_and_weight[0])
                    vweight = float(seed_and_weight[1])
                except ValueError:
                    print(f'could not parse with_variation part "{part}"')
                    broken = True
                    break
                parts.append([vseed, vweight])
            if broken:
                raise CanceledException
            if len(parts) > 0:
                with_variations = parts
            else:
                with_variations = None

        self.canceled.clear()
        print(f">> Request to generate with prompt: {prompt}")
        # In order to handle upscaled images, the PngWriter needs to maintain state
        # across images generated by each call to prompt2img(), so we define it in
        # the outer scope of image_done()
        config = post_data.copy() # Shallow copy
        config['initimg'] = config.pop('initimg_name','')

        images_generated = 0    # helps keep track of when upscaling is started
        images_upscaled = 0     # helps keep track of when upscaling is completed
        pngwriter = PngWriter(self.outdir)

        prefix = pngwriter.unique_prefix()
        # if upscaling is requested, then this will be called twice, once when
        # the images are first generated, and then again when after upscaling
        # is complete. The upscaling replaces the original file, so the second
        # entry should not be inserted into the image list.
        def image_done(image, seed, upscaled=False):
            name = f'{prefix}.{seed}.png'
            path = pngwriter.save_image_and_prompt_to_png(image, f'{prompt} -S{seed}', name)

            config['seed'] = seed
            # Append post_data to log, but only once!
            if not upscaled:
                with open(os.path.join(self.outdir, "dream_web_log.txt"), "a") as log:
                    log.write(f"{path}: {json.dumps(config)}\n")

                self.wfile.write(bytes(json.dumps(
                    {'event': 'result', 'url': path, 'seed': seed, 'config': config}
                ) + '\n',"utf-8"))

            # control state of the "postprocessing..." message
            upscaling_requested = upscale or gfpgan_strength>0
            nonlocal images_generated # NB: Is this bad python style? It is typical usage in a perl closure.
            nonlocal images_upscaled  # NB: Is this bad python style? It is typical usage in a perl closure.
            if upscaled:
                images_upscaled += 1
            else:
                images_generated +=1
            if upscaling_requested:
                action = None
                if images_generated >= iterations:
                    if images_upscaled < iterations:
                        action = 'upscaling-started'
                    else:
                        action = 'upscaling-done'
                if action:
                    x = images_upscaled+1
                    self.wfile.write(bytes(json.dumps(
                        {'event':action,'processed_file_cnt':f'{x}/{iterations}'}
                    ) + '\n',"utf-8"))

        step_writer = PngWriter(os.path.join(self.outdir, "intermediates"))
        step_index = 1
        def image_progress(sample, step):
            if self.canceled.is_set():
                self.wfile.write(bytes(json.dumps({'event':'canceled'}) + '\n', 'utf-8'))
                raise CanceledException
            path = None
            # since rendering images is moderately expensive, only render every 5th image
            # and don't bother with the last one, since it'll render anyway
            nonlocal step_index
            if progress_images and step % 5 == 0 and step < steps - 1:
                image = self.model.sample_to_image(sample)
                name = f'{prefix}.{seed}.{step_index}.png'
                metadata = f'{prompt} -S{seed} [intermediate]'
                path = step_writer.save_image_and_prompt_to_png(image, metadata, name)
                step_index += 1
            self.wfile.write(bytes(json.dumps(
                {'event': 'step', 'step': step + 1, 'url': path}
            ) + '\n',"utf-8"))

        try:
            if initimg is None:
                # Run txt2img
                self.model.prompt2image(prompt,
                                        iterations=iterations,
                                        cfg_scale = cfgscale,
                                        width = width,
                                        height = height,
                                        seed = seed,
                                        steps = steps,
                                        variation_amount = variation_amount,
                                        with_variations = with_variations,
                                        gfpgan_strength = gfpgan_strength,
                                        upscale         = upscale,
                                        sampler_name    = sampler_name,
                                        seamless        = seamless,
                                        step_callback=image_progress,
                                        image_callback=image_done)
            else:
                # Decode initimg as base64 to temp file
                with open("./img2img-tmp.png", "wb") as f:
                    initimg = initimg.split(",")[1] # Ignore mime type
                    f.write(base64.b64decode(initimg))

                try:
                    # Run img2img
                    self.model.prompt2image(prompt,
                                            init_img = "./img2img-tmp.png",
                                            strength = strength,
                                            iterations = iterations,
                                            cfg_scale  = cfgscale,
                                            seed       = seed,
                                            steps      = steps,
                                            variation_amount = variation_amount,
                                            with_variations = with_variations,
                                            sampler_name    = sampler_name,
                                            width      = width,
                                            height     = height,
                                            fit        = fit,
                                            seamless   = seamless,
                                            gfpgan_strength=gfpgan_strength,
                                            upscale         = upscale,
                                            step_callback=image_progress,
                                            image_callback=image_done)
                finally:
                    # Remove the temp file
                    os.remove("./img2img-tmp.png")
        except CanceledException:
            print(f"Canceled.")
            return


class ThreadingDreamServer(ThreadingHTTPServer):
    def __init__(self, server_address):
        super(ThreadingDreamServer, self).__init__(server_address, DreamServer)
