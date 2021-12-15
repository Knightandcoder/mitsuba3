#include <mitsuba/core/bitmap.h>
#include <mitsuba/render/imageblock.h>
#include <mitsuba/python/python.h>

MTS_PY_EXPORT(ImageBlock) {
    MTS_PY_IMPORT_TYPES(ImageBlock, ReconstructionFilter)
    MTS_PY_CLASS(ImageBlock, Object)
        .def(py::init<const ScalarPoint2u &, const ScalarVector2u &, uint32_t,
                      const ReconstructionFilter *, bool, bool, bool, bool,
                      bool>(),
             "offset"_a, "size"_a, "channel_count"_a, "rfilter"_a = nullptr,
             "border"_a = std::is_scalar_v<Float>, "normalize"_a = false,
             "coalesce"_a      = ek::is_llvm_array_v<Float>,
             "warn_negative"_a = std::is_scalar_v<Float>,
             "warn_invalid"_a  = std::is_scalar_v<Float>)
        .def(py::init<const ScalarPoint2u &, const TensorXf &,
                      const ReconstructionFilter *, bool, bool, bool, bool,
                      bool>(),
             "offset"_a, "tensor"_a, "rfilter"_a = nullptr,
             "border"_a = std::is_scalar_v<Float>, "normalize"_a = false,
             "coalesce"_a      = ek::is_llvm_array_v<Float>,
             "warn_negative"_a = std::is_scalar_v<Float>,
             "warn_invalid"_a  = std::is_scalar_v<Float>)
        .def("put_block", &ImageBlock::put_block, D(ImageBlock, put), "block"_a)
        .def("put",
             py::overload_cast<const Point2f &, const wavelength_t<Spectrum> &,
                               const Spectrum &, const Float &, const Float &,
                               ek::mask_t<Float>>(&ImageBlock::put),
             "pos"_a, "wavelengths"_a, "value"_a, "alpha"_a = 1.f,
             "weight"_a = 1, "active"_a = true, D(ImageBlock, put, 2))
        .def("put",
             [](ImageBlock &ib, const Point2f &pos,
                const std::vector<Float> &values, Mask active) {
                 if (values.size() != ib.channel_count())
                     throw std::runtime_error("Incompatible channel count!");
                 ib.put(pos, values.data(), active);
             }, "pos"_a, "values"_a, "active"_a = true)
        .def("read",
             [](ImageBlock &ib, const Point2f &pos, Mask active) {
                 std::vector<Float> values(ib.channel_count());
                 ib.read(pos, values.data(), active);
                 return values;
             }, "pos"_a, "active"_a = true)
        .def_method(ImageBlock, clear)
        .def_method(ImageBlock, set_offset, "offset"_a)
        .def_method(ImageBlock, offset)
        .def_method(ImageBlock, size)
        .def_method(ImageBlock, width)
        .def_method(ImageBlock, height)
        .def_method(ImageBlock, rfilter)
        .def_method(ImageBlock, coalesce)
        .def_method(ImageBlock, normalize)
        .def_method(ImageBlock, warn_invalid)
        .def_method(ImageBlock, warn_negative)
        .def_method(ImageBlock, set_warn_invalid, "value"_a)
        .def_method(ImageBlock, set_warn_negative, "value"_a)
        .def_method(ImageBlock, border_size)
        .def_method(ImageBlock, channel_count)
        .def("tensor", py::overload_cast<>(&ImageBlock::tensor),
             py::return_value_policy::reference_internal,
             D(ImageBlock, tensor));
}
