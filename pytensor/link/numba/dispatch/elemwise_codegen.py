from llvmlite import ir
from numba import types
from numba.np import arrayobj
from numba.core import cgutils
import numba
import numpy as np


def compute_itershape(
    ctx,
    builder: ir.IRBuilder,
    in_shapes,
    broadcast_pattern,
):
    one = ir.IntType(64)(1)
    ndim = len(in_shapes[0])
    #shape = [ir.IntType(64)(1) for _ in range(ndim)]
    shape = [None] * ndim
    for i in range(ndim):
        # TODO Error checking...
        # What if all shapes are 0?
        for bc, in_shape in zip(broadcast_pattern, in_shapes):
            if bc[i]:
                # TODO
                # raise error if length != 1
                pass
            else:
                # TODO
                # if shape[i] is not None:
                #     raise Error if !=
                shape[i] = in_shape[i]
    for i in range(ndim):
        if shape[i] is None:
            shape[i] = one
    return shape


def make_outputs(ctx, builder: ir.IRBuilder, iter_shape, out_bc, dtypes, inplace, inputs, input_types):
    arrays = []
    ar_types: list[types.Array] = []
    one = ir.IntType(64)(1)
    inplace = dict(inplace)
    for i, (bc, dtype) in enumerate(zip(out_bc, dtypes)):
        if i in inplace:
            arrays.append(inputs[inplace[i]])
            ar_types.append(input_types[inplace[i]])
            # We need to incref once we return the inplace objects
            continue
        dtype = numba.from_dtype(np.dtype(dtype))
        arrtype = types.Array(dtype, len(iter_shape), "C")
        ar_types.append(arrtype)
        # This is actually an interal numba function, I guess we could
        # call `numba.nd.unsafe.ndarray` instead?
        shape = [
            length if not bc_dim else one
            for length, bc_dim in zip(iter_shape, bc)
        ]
        array = arrayobj._empty_nd_impl(ctx, builder, arrtype, shape)
        arrays.append(array)

    # If there is no inplace operation, we know that all output arrays
    # don't alias. Informing llvm can make it easier to vectorize.
    if not inplace:
        # The first argument is the output pointer
        arg = builder.function.args[0]
        arg.add_attribute("noalias")
    return arrays, ar_types


def make_loop_call(
    typingctx,
    context: numba.core.base.BaseContext,
    builder: ir.IRBuilder,
    scalar_func,
    scalar_signature,
    iter_shape,
    inputs,
    outputs,
    input_bc,
    output_bc,
    input_types,
    output_types,
):
    safe = (False, False)
    n_outputs = len(outputs)

    #context.printf(builder, "iter shape: " + ', '.join(["%i"] * len(iter_shape)) + "\n", *iter_shape)

    # Lower the code of the scalar function so that we can use it in the inner loop
    # Caching is set to false to avoid a numba bug TODO ref?
    inner_func = context.compile_subroutine(
        builder,
        # I don't quite understand why we need to access `dispatcher` here.
        # The object does seem to be a dispatcher already? But it is missing
        # attributes...
        scalar_func.dispatcher,
        scalar_signature,
        caching=False,
    )
    inner = inner_func.fndesc

    # Extract shape and stride information from the array.
    # For later use in the loop body to do the indexing
    def extract_array(aryty, obj):
        shape = cgutils.unpack_tuple(builder, obj.shape)
        strides = cgutils.unpack_tuple(builder, obj.strides)
        data = obj.data
        layout = aryty.layout
        return (data, shape, strides, layout)

    # TODO I think this is better than the noalias attribute
    # for the input, but self_ref isn't supported in a released
    # llvmlite version yet
    # mod = builder.module
    # domain = mod.add_metadata([], self_ref=True)
    # input_scope = mod.add_metadata([domain], self_ref=True)
    # output_scope = mod.add_metadata([domain], self_ref=True)
    # input_scope_set = mod.add_metadata([input_scope, output_scope])
    # output_scope_set = mod.add_metadata([input_scope, output_scope])

    inputs = [
        extract_array(aryty, ary)
        for aryty, ary in zip(input_types, inputs, strict=True)
    ]

    outputs = [
        extract_array(aryty, ary)
        for aryty, ary in zip(output_types, outputs, strict=True)
    ]

    zero = ir.Constant(ir.IntType(64), 0)

    # Setup loops and initialize accumulators for outputs
    # This part corresponds to opening the loops
    loop_stack = []
    loops = []
    output_accumulator = [(None, None)] * n_outputs
    for dim, length in enumerate(iter_shape):
        # Find outputs that only have accumulations left
        for output in range(n_outputs):
            if output_accumulator[output][0] is not None:
                continue
            if all(output_bc[output][dim:]):
                value = outputs[output][0].type.pointee(0)
                accu = cgutils.alloca_once_value(builder, value)
                output_accumulator[output] = (accu, dim)

        loop = cgutils.for_range(builder, length)
        loop_stack.append(loop)
        loops.append(loop.__enter__())

    # Code in the inner most loop...
    idxs = [loopval.index for loopval in loops]

    # Load values from input arrays
    input_vals = []
    for array_info, bc in zip(inputs, input_bc, strict=True):
        idxs_bc = [
            zero if bc else idx for idx, bc in zip(idxs, bc, strict=True)
        ]
        ptr = cgutils.get_item_pointer2(
            context, builder, *array_info, idxs_bc, *safe
        )
        val = builder.load(ptr)
        # val.set_metadata("alias.scope", input_scope_set)
        # val.set_metadata("noalias", output_scope_set)
        input_vals.append(val)

    # Call scalar function
    output_values = context.call_internal(
        builder,
        inner,
        scalar_signature,
        input_vals,
    )
    if isinstance(scalar_signature.return_type, types.Tuple):
        output_values = cgutils.unpack_tuple(builder, output_values)
    else:
        output_values = [output_values]

    # Update output value or accumulators respectively
    for i, ((accu, _), value) in enumerate(
        zip(output_accumulator, output_values, strict=True)
    ):
        if accu is not None:
            load = builder.load(accu)
            # load.set_metadata("alias.scope", output_scope_set)
            # load.set_metadata("noalias", input_scope_set)
            new_value = builder.fadd(load, value)
            builder.store(new_value, accu)
            # TODO belongs to noalias scope
            # store.set_metadata("alias.scope", output_scope_set)
            # store.set_metadata("noalias", input_scope_set)
        else:
            idxs_bc = [
                zero if bc else idx
                for idx, bc in zip(idxs, output_bc[i], strict=True)
            ]
            ptr = cgutils.get_item_pointer2(
                context, builder, *outputs[i], idxs_bc
            )
            # store = builder.store(value, ptr)
            arrayobj.store_item(context, builder, output_types[i], value, ptr)
            # store.set_metadata("alias.scope", output_scope_set)
            # store.set_metadata("noalias", input_scope_set)

    # Close the loops and write accumulator values to the output arrays
    for depth, loop in enumerate(loop_stack[::-1]):
        for output, (accu, accu_depth) in enumerate(output_accumulator):
            if accu_depth == depth:
                idxs_bc = [
                    zero if bc else idx
                    for idx, bc in zip(
                        idxs, output_bc[output], strict=True
                    )
                ]
                ptr = cgutils.get_item_pointer2(
                    context, builder, *outputs[output], idxs_bc
                )
                load = builder.load(accu)
                # load.set_metadata("alias.scope", output_scope_set)
                # load.set_metadata("noalias", input_scope_set)
                # store = builder.store(load, ptr)
                arrayobj.store_item(
                    context, builder, output_types[output], load, ptr
                )
                # store.set_metadata("alias.scope", output_scope_set)
                # store.set_metadata("noalias", input_scope_set)
        loop.__exit__(None, None, None)

    return