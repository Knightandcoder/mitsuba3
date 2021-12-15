from __future__ import annotations # Delayed parsing of type annotations

import time
import enoki as ek
import mitsuba
from .common import prepare_sampler, sample_sensor_rays, mis_weight

from typing import Union

class PRBReparamIntegrator(mitsuba.render.SamplingIntegrator):
    """
    This integrator implements a path replay backpropagation surface path tracer
    with discontinuities reparameterization.
    """

    def __init__(self, props=mitsuba.core.Properties()):
        super().__init__(props)
        self.max_depth = props.get('max_depth', 4)
        self.max_depth_reparam = props.get('max_depth_reparam', self.max_depth)
        self.num_aux_rays = props.get('num_aux_rays', 16)
        self.kappa = props.get('kappa', 1e5)
        self.power = props.get('power', 3.0)

    def render_forward(self: mitsuba.render.SamplingIntegrator,
                       scene: mitsuba.render.Scene,
                       params: mitsuba.python.util.SceneParameters,
                       sensor: Union[int, mitsuba.render.Sensor] = 0,
                       seed: int = 0,
                       spp: int = 0) -> mitsuba.core.TensorXf:
        from mitsuba.core import Float, Spectrum, Log, LogLevel, util
        from mitsuba.render import ImageBlock, Interaction3f
        from mitsuba.python.ad import reparameterize_ray

        Log(LogLevel.Info, 'start rendering ..')
        starting_time = time.time()

        if isinstance(sensor, int):
            sensor = scene.sensors()[sensor]
        film = sensor.film()
        rfilter = film.rfilter()
        sampler = sensor.sampler()

        assert not rfilter.class_().name() == 'BoxFilter'
        assert film.sample_border()

        # Seed the sampler and compute the number of sample per pixels
        spp = prepare_sampler(sensor, seed, spp)

        ray, weight, pos, aperture_samples = sample_sensor_rays(sensor)

        # Sample forward paths (not differentiable)
        with ek.suspend_grad():
            Li = self.Li(None, scene, sampler.clone(), ray)[0]
        ek.eval(Li)

        grad_img = self.Li(ek.ADMode.Forward, scene, sampler,
                           ray, params=params, grad=weight,
                           primal_result=Spectrum(Li))[0]
        sampler.schedule_state()
        ek.eval(grad_img)

        # Reparameterize primary rays
        reparam_d, reparam_div = reparameterize_ray(scene, sampler, ray, params,
                                                    True, self.num_aux_rays,
                                                    self.kappa, self.power)
        it = ek.zero(Interaction3f)
        it.p = ray.o + reparam_d
        ds, w_reparam = sensor.sample_direction(it, aperture_samples)
        w_reparam = ek.select(w_reparam > 0.0, w_reparam / ek.detach(w_reparam), 1.0)

        block = ImageBlock(film.crop_offset(), film.crop_size(),
                           channel_count=5, rfilter=rfilter, border=True)
        block.put(ds.uv, ray.wavelengths, Li * w_reparam)
        film.prepare([])
        film.put(block)
        Li_attached = film.develop()

        ek.enqueue(ek.ADMode.Forward, params)
        ek.traverse(Float)

        div_grad = weight * Li * ek.grad(reparam_div)
        Li_grad = ek.grad(Li_attached)
        ek.eval(div_grad, Li_grad)

        block.clear()
        block.put(pos, ray.wavelengths, grad_img + div_grad)
        film.prepare([])
        film.put(block)

        grad_out = Li_grad + film.develop()

        Log(LogLevel.Info, 'rendering finished. (took %s)' %
            util.time_string(1e3 * (time.time() - starting_time)))

        return grad_out

    def render_backward(self: mitsuba.render.SamplingIntegrator,
                        scene: mitsuba.render.Scene,
                        params: mitsuba.python.util.SceneParameters,
                        image_adj: mitsuba.core.TensorXf,
                        sensor: Union[int, mitsuba.render.Sensor] = 0,
                        seed: int = 0,
                        spp: int = 0) -> None:
        from mitsuba.core import Float, Spectrum, Log, LogLevel
        from mitsuba.render import ImageBlock, Interaction3f
        from mitsuba.python.ad import reparameterize_ray

        Log(LogLevel.Info, 'start rendering ..')
        starting_time = time.time()

        if isinstance(sensor, int):
            sensor = scene.sensors()[sensor]
        film = sensor.film()
        rfilter = film.rfilter()
        sampler = sensor.sampler()

        assert not rfilter.class_().name() == 'BoxFilter'
        assert film.sample_border()

        # Seed the sampler and compute the number of sample per pixels
        spp = prepare_sampler(sensor, seed, spp)

        ray, weight, pos, aperture_samples = sample_sensor_rays(sensor)

        # Read image gradient values per sample through the pixel filter
        block = ImageBlock(film.crop_offset(), ek.detach(image_adj), rfilter, normalize=True)
        grad = Spectrum(block.read(pos)) * weight / spp

        # Sample forward paths (not differentiable)
        with ek.suspend_grad():
            Li = self.Li(None, scene, sampler.clone(), ray)[0]
            sampler.schedule_state()
            ek.eval(Li, grad)

        # Replay light paths by using the same seed and accumulate gradients
        # This uses the result from the first pass to compute gradients
        self.Li(ek.ADMode.Backward, scene, sampler, ray,
                params=params, grad=grad, primal_result=Spectrum(Li))
        sampler.schedule_state()
        ek.eval()

        # Reparameterize primary rays
        reparam_d, reparam_div = reparameterize_ray(scene, sampler, ray, params,
                                                    True, self.num_aux_rays,
                                                    self.kappa, self.power)
        it = ek.zero(Interaction3f)
        it.p = ray.o + reparam_d
        ds, w_reparam = sensor.sample_direction(it, aperture_samples)
        w_reparam = ek.select(w_reparam > 0.0, w_reparam / ek.detach(w_reparam), 1.0)

        block = ImageBlock(film.crop_offset(), film.crop_size(),
                           channel_count=5, rfilter=rfilter, border=True)
        block.put_block(ds.uv, ray.wavelengths, Li * w_reparam)
        film.prepare([])
        film.put(block)
        Li_attached = film.develop()

        ek.set_grad(Li_attached, image_adj)
        ek.set_grad(reparam_div, ek.hsum(grad * Li))
        ek.enqueue(ek.ADMode.Backward, Li_attached, reparam_div)
        ek.traverse(Float)

        Log(LogLevel.Info, 'rendering finished. (took %s)' %
            mitsuba.core.util.time_string(1e3 * (time.time() - starting_time)))

    def sample(self, scene, sampler, ray, medium, active):
        res, valid = self.Li(None, scene, sampler, ray, active_=active)
        return res, valid, []

    def Li(self: mitsuba.render.SamplingIntegrator,
           mode: ek.ADMode,
           scene: mitsuba.render.Scene,
           sampler: mitsuba.render.Sampler,
           ray_: mitsuba.core.RayDifferential3f,
           depth: mitsuba.core.UInt32=1,
           params=mitsuba.python.util.SceneParameters(),
           grad: mitsuba.core.Spectrum=None,
           active_: mitsuba.core.Mask=True,
           primal_result: mitsuba.core.Spectrum=None):
        from mitsuba.core import Spectrum, Float, Mask, UInt32, Ray3f, Loop
        from mitsuba.render import DirectionSample3f, BSDFContext, BSDFFlags, has_flag, RayFlags
        from mitsuba.python.ad import reparameterize_ray

        is_primal = mode is None

        def reparam(ray, active):
            return reparameterize_ray(scene, sampler, ray, params, active,
                                      num_auxiliary_rays=self.num_aux_rays,
                                      kappa=self.kappa, power=self.power)

        ray = Ray3f(ray_)
        pi = scene.ray_intersect_preliminary(ray, active_)
        valid_ray = active_ & pi.is_valid()

        result = Spectrum(0.0)
        if is_primal:
            primal_result = Spectrum(0.0)

        throughput = Spectrum(1.0)
        active = Mask(active_)
        emission_weight = Float(1.0)

        depth_i = UInt32(depth)
        loop = Loop("Path Replay Backpropagation main loop" + '' if is_primal else ' - adjoint')
        loop.put(lambda: (depth_i, active, ray, emission_weight,
                          throughput, pi, result, primal_result))
        sampler.loop_register(loop)
        loop.init()
        while loop(active):
            # Attach incoming direction (reparameterization from the previous bounce)
            si = pi.compute_surface_interaction(ray, RayFlags.All, active)
            reparam_d, _ = reparam(ray, active & ((depth_i-1) < self.max_depth_reparam))
            si.wi = -ek.select(active & si.is_valid(), si.to_local(reparam_d), reparam_d)

            # ---------------------- Direct emission ----------------------

            emitter_val = si.emitter(scene, active).eval(si, active)
            accum = emitter_val * throughput * emission_weight

            active &= si.is_valid()
            active &= depth_i < self.max_depth

            ctx = BSDFContext()
            bsdf = si.bsdf(ray)

            # ---------------------- Emitter sampling ----------------------

            active_e = active & has_flag(bsdf.flags(), BSDFFlags.Smooth)
            ds, emitter_val = scene.sample_emitter_direction(
                ek.detach(si), sampler.next_2d(active_e), True, active_e)
            ds = ek.detach(ds, True)
            active_e &= ek.neq(ds.pdf, 0.0)

            ray_e = ek.detach(si.spawn_ray(ds.d))
            reparam_d, reparam_div = reparam(ray_e, active_e & (depth_i < self.max_depth_reparam))
            wo = si.to_local(reparam_d)

            bsdf_val, bsdf_pdf = bsdf.eval_pdf(ctx, si, wo, active_e)
            mis = ek.select(ds.delta, 1.0, mis_weight(ds.pdf, bsdf_pdf))

            contrib = bsdf_val * emitter_val * throughput * mis
            accum += ek.select(active_e, contrib, 0.0)

            # Update accumulated radiance. When propagating gradients, we subtract the
            # emitter contributions instead of adding them
            if not is_primal:
                primal_result -= ek.detach(accum)

            accum += ek.select(active_e, reparam_div * ek.detach(contrib), 0.0)

            # ---------------------- BSDF sampling ----------------------

            with ek.suspend_grad():
                bs, bsdf_weight = bsdf.sample(ctx, ek.detach(si),
                                              sampler.next_1d(active),
                                              sampler.next_2d(active), active)
                active &= bs.pdf > 0.0
                ray = ek.detach(si.spawn_ray(si.to_world(bs.wo)))
                pi_bsdf = scene.ray_intersect_preliminary(ray, active)
                si_bsdf = pi_bsdf.compute_surface_interaction(ray, RayFlags.All, active)

            # Compute MIS weight for the BSDF sampling
            ds = DirectionSample3f(scene, si_bsdf, si)
            ds.emitter = si_bsdf.emitter(scene, active)
            delta = has_flag(bs.sampled_type, BSDFFlags.Delta)
            emitter_pdf = scene.pdf_emitter_direction(si, ds, ~delta)
            emission_weight = ek.select(delta, 1.0, mis_weight(bs.pdf, emitter_pdf))

            # Backpropagate gradients related to the current bounce
            if not is_primal:
                reparam_d, reparam_div = reparam(ray, active & (depth_i < self.max_depth_reparam))
                bsdf_eval = bsdf.eval(ctx, si, si.to_local(reparam_d), active)

                contrib = bsdf_eval * primal_result / ek.max(1e-8, ek.detach(bsdf_eval))
                accum += ek.select(active, contrib + reparam_div * ek.detach(contrib), 0.0)

            if mode is ek.ADMode.Backward:
                ek.backward(accum * grad, ek.ADFlag.ClearVertices)
            elif mode is ek.ADMode.Forward:
                ek.enqueue(ek.ADMode.Forward, params)
                ek.traverse(Float, ek.ADFlag.ClearEdges | ek.ADFlag.ClearInterior)
                result += ek.grad(accum) * grad
            else:
                result += accum

            pi = pi_bsdf
            throughput *= bsdf_weight

            depth_i += UInt32(1)

        return result, valid_ray

    def to_string(self):
        return f'PRBReparamIntegrator[max_depth = {self.max_depth}]'


mitsuba.render.register_integrator("prbreparam", lambda props: PRBReparamIntegrator(props))
