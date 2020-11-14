"""
  Copyright 2020 ETH Zurich, Secure, Reliable, and Intelligent Systems Lab

  Licensed under the Apache License, Version 2.0 (the "License");
  you may not use this file except in compliance with the License.
  You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

  Unless required by applicable law or agreed to in writing, software
  distributed under the License is distributed on an "AS IS" BASIS,
  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
  See the License for the specific language governing permissions and
  limitations under the License.
"""

import numpy as np
import onnx
import math
from onnx import numpy_helper
from config import config


def onnxshape_to_intlist(onnxshape):
    """
	ONNX has its own wrapper for shapes. Our optimizer expects a list of ints.

	Arguments
	---------
	onnxshape : TensorShapeProto

	Return
	------
	output : list
	    list of ints corresponding to onnxshape
	"""
    result = list(map(lambda j: 1 if j.dim_value is None else int(j.dim_value), onnxshape.dim))

    # No shape means a single value
    if not result:
        return [1]

    # convert NCHW to NHWC
    if len(result) == 4:
        return [result[0], result[2], result[3], result[1]]

    return result


def nchw_to_nhwc(array):
    """
	ONNX uses NCHW. ELINA expects NHWC

	:param array: array to be converted

	:return: converted array
	"""
    if array.ndim == 4:
        return array.transpose(0, 2, 3, 1)

    return array


def reshape_nhwc(shape_in, shape_out):
    # print(shape_in, shape_out)
    ndim_in = len(shape_in)
    ndim_out = len(shape_out)
    total_in = np.prod(shape_in[1:ndim_in])
    total_out = np.prod(shape_out[1:ndim_out])
    assert total_in == total_out, "Reshape doesn't have same number of neurons before and after"
    array = np.asarray(range(total_in)).reshape(shape_in[1:ndim_in])
    if array.ndim == 3:
        array = array.transpose((2, 0, 1))
    array = array.reshape(shape_out[1:ndim_out])
    if array.ndim == 3:
        return array.transpose((1, 2, 0))
    else:
        return array


def prepare_model(model):
    """
	The constructor has produced a graph_def with the help of the functions graph_util.convert_variables_to_constants and graph_util.remove_training_nodes.
	translate() takes that graph_def, imports it, and translates it into two lists which then can be processed by an Optimzer object.

	Return
	------
	(operation_types, operation_resources) : (list, list)
	    A tuple with two lists, the first one has items of type str and the second one of type dict. In the first list the operation types are stored (like "Add", "MatMul", etc.).
	    In the second list we store the resources (matrices, biases, etc.) for those operations. It is organised as follows: operation_resources[i][domain] has the resources related to
	    operation_types[i] when analyzed with domain (domain is currently either 'deepzono' or 'deeppoly', as of 8/30/18)
	"""
    shape_map = {}
    constants_map = {}
    nchw_cons_map = {}
    output_node_map = {}
    input_node_map = {}
    BN_map = {}
    for initial in model.graph.initializer:
        const_nchw = numpy_helper.to_array(initial)
        const = nchw_to_nhwc(numpy_helper.to_array(initial)).copy()
        constants_map[initial.name] = const
        nchw_cons_map[initial.name] = const_nchw
        #print("Key: ", initial.name)
        shape_map[initial.name] = const.shape

    placeholdernames = []
    # print("graph ", model.graph.input)
    # First handle the network input?
    for input in model.graph.input:
        placeholdernames.append(input.name)
        if input.name not in shape_map:
            shape_map[input.name] = onnxshape_to_intlist(input.type.tensor_type.shape)
            input_node_map[input.name] = input

    for node in model.graph.node:
        # print(node.op_type)
        output_node_map[node.output[0]] = node
        for input in node.input:
            input_node_map[input] = node
        if node.op_type == "Flatten":
            shape_map[node.output[0]] = shape_map[node.input[0]]
        elif node.op_type == "Constant":
            const = node.attribute
            const = nchw_to_nhwc(numpy_helper.to_array(const[0].t))
            constants_map[node.output[0]] = const
            shape_map[node.output[0]] = const.shape

        elif node.op_type in ["MatMul", "Gemm"]:
            transA = 0
            transB = 0
            for attribute in node.attribute:
                if 'transA' == attribute.name:
                    transA = attribute.i
                elif 'transB' == attribute.name:
                    transB = attribute.i
            M = shape_map[node.input[0]][transA]
            if len(shape_map[node.input[1]]) == 1 and transB == 0:
                N = 1
            else:
                N = shape_map[node.input[1]][1 - transB]
            shape_map[node.output[0]] = [M, N]

        elif node.op_type in ["Add", "Sub", "Mul"]:
            shape_map[node.output[0]] = shape_map[node.input[0]]
            if node.input[0] in constants_map and node.input[1] in constants_map:
                if node.op_type == "Add":
                    result = np.add(constants_map[node.input[0]], constants_map[node.input[1]])
                elif node.op_type == "Sub":
                    result = np.subtract(constants_map[node.input[0]], constants_map[node.input[1]])
                elif node.op_type == "Mul":
                    result = np.multiply(constants_map[node.input[0]], constants_map[node.input[1]])
                constants_map[node.output[0]] = result
        elif node.op_type in ["Conv", "MaxPool", "AveragePool"]:
            output_shape = []
            # This is the input shape, which is the shape of X in Conv actually, yes it is!!
            input_shape = shape_map[node.input[0]]
            require_kernel_shape = node.op_type in ["MaxPool", "AveragePool"]
            if not require_kernel_shape:
                # if Conv layers
                filter_shape = shape_map[node.input[1]]
                kernel_shape = filter_shape[1:-1]
                #print("filter shape,", filter_shape)
                #print("First filter,", nchw_cons_map[node.input[1]][0])
            strides = [1, 1]
            padding = [0, 0, 0, 0]
            auto_pad = 'NOTSET'
            dilations = [1, 1]
            group = 1
            ceil_mode = 0
            for attribute in node.attribute:
                # Get all the attibute values
                if attribute.name == 'strides':
                    strides = attribute.ints
                elif attribute.name == 'pads':
                    padding = attribute.ints
                elif attribute.name == 'auto_pad':
                    auto_pad = attribute.s
                elif attribute.name == 'kernel_shape':
                    kernel_shape = attribute.ints
                elif attribute.name == 'dilations':
                    dilations = attribute.ints
                elif attribute.name == 'group':
                    group = attribute.i
                elif attribute.name == 'ceil_mode':
                    ceil_mode = attribute.i

            effective_kernel_shape = [(kernel_shape[i] - 1) * dilations[i] + 1 for i in range(len(kernel_shape))]
            output_shape.append(input_shape[0])

            for i in range(len(kernel_shape)):
                effective_input_size = input_shape[1 + i]
                effective_input_size += padding[i]
                effective_input_size += padding[i + len(kernel_shape)]
                #print("effective_input_size", effective_input_size)
                if ceil_mode == 1:
                    strided_kernel_positions = int(
                        np.ceil((effective_input_size - effective_kernel_shape[i]) / float(strides[i])))
                else:
                    #Enter here for Conv in FFDNet
                    strided_kernel_positions = int(
                        np.floor((effective_input_size - effective_kernel_shape[i]) / strides[i]))
                    #print("strided_kernel_positions", strided_kernel_positions)
                output_shape.append(1 + strided_kernel_positions)

            if require_kernel_shape:
                output_shape.append(input_shape[1])
            else:
                # The Conv layers
                output_shape.append(filter_shape[0])
            #print("output shape",output_shape)
            shape_map[node.output[0]] = output_shape
        elif node.op_type in ["Relu", "Sigmoid", "Tanh", "Softmax"]:
            # Originally include BN layer in there
            shape_map[node.output[0]] = shape_map[node.input[0]]
        elif node.op_type == "BatchNormalization":
            # Store the node information using the name of input, so that we can check if this name is the output of previous Conv layers
            BN_map[node.input[0]] = node
            shape_map[node.output[0]] = shape_map[node.input[0]]
            '''
            input_x = node.input[0]
            input_x_shape = shape_map[node.input[0]]
            #print("BN, the input shape of X is ", input_x_shape)
            scale_para = constants_map[node.input[1]]
            bias_para = constants_map[node.input[2]]
            mean_para = constants_map[node.input[3]]
            var_para = constants_map[node.input[4]]
            '''
            scale_para = nchw_cons_map[node.input[1]][0]
            #print("BN, first gamma,", scale_para)
            for attribute in node.attribute:
                # Get all the attibute values
                #print("attribute name ", attribute.name)
                if attribute.name == 'epsilon':
                    epsilon = attribute.f
                elif attribute.name == 'momentum':
                    momentum = attribute.f
        elif node.op_type == "Gather":
        # Gather is for the moment solely for shapes
            axis = 0
            for attribute in node.attribute:
                axis = attribute.i
            if node.input[0] in constants_map and node.input[1] in constants_map:
                data = constants_map[node.input[0]]
                indexes = constants_map[node.input[1]]
                constants_map[node.output[0]] = np.take(data, indexes, axis)

            if node.input[0] in shape_map and node.input[1] in shape_map:
                r = len(shape_map[node.input[0]])
                q = len(shape_map[node.input[1]])
                out_rank = q + r - 1
                if out_rank == 0:
                    shape_map[node.output[0]] = shape_map[node.input[1]]
                else:
                    output_shape = []
                    for i in range(out_rank):
                        if i < axis:
                            output_shape.append(shape_map[node.input[0]][i])  # i < axis < r
                        elif i >= axis and i < axis + q:
                            output_shape.append(shape_map[node.input[0]][i - axis])  # i - axis < q
                        else:
                            output_shape.append(shape_map[node.input[0]][i - q + 1])  # i < out_rank < q + r - 1
                    shape_map[node.output[0]] = output_shape
        elif node.op_type == "Shape":
            if node.input[0] in shape_map:
                constants_map[node.output[0]] = shape_map[node.input[0]]
                shape_map[node.output[0]] = [len(shape_map[node.input[0]])]

        elif node.op_type == "Reshape":
            if node.input[1] in constants_map:
                total = 1
                replace_index = -1
                for index in range(len(constants_map[node.input[1]])):
                    if constants_map[node.input[1]][index] == -1:
                        replace_index = index
                    else:
                        total *= constants_map[node.input[1]][index]

                if replace_index != -1:
                    constants_map[node.input[1]][replace_index] = np.prod(shape_map[node.input[0]]) / total

                if len(constants_map[node.input[1]]) == 4:
                    shape_map[node.output[0]] = [constants_map[node.input[1]][0], constants_map[node.input[1]][2],
                                                 constants_map[node.input[1]][3], constants_map[node.input[1]][1]]
                else:
                    shape_map[node.output[0]] = constants_map[node.input[1]]

        elif node.op_type == "Unsqueeze":
            if node.input[0] in shape_map:
                axis = node.attribute[0].ints
                output_shape = list(shape_map[node.input[0]])
                if node.input[0] in constants_map:
                    constants_map[node.output[0]] = constants_map[node.input[0]]
                for i in axis:
                    output_shape.insert(i, 1)
                    if node.input[0] in constants_map:
                        constants_map[node.output[0]] = np.expand_dims(constants_map[node.output[0]], axis=i)
                shape_map[node.output[0]] = output_shape

        elif node.op_type == "Concat":
            all_constant = True
            axis = node.attribute[0].i
            for input in node.input:
                if not input in constants_map:
                    all_constant = False
                    break
            if all_constant:
                constants_map[node.output[0]] = np.concatenate([constants_map[input] for input in node.input],
                                                               axis=axis)

            all_shape_known = True
            for input in node.input:
                if not input in shape_map:
                    all_shape_known = False
                    break
            if all_shape_known:
                new_axis_size = 0
                for input in node.input:
                    new_axis_size += shape_map[input][axis]
                shape_map[node.output[0]] = [shape_map[node.input[0]][i] if i != axis else new_axis_size for i in
                                             range(len(shape_map[node.input[0]]))]

        elif node.op_type == "Expand":
            if node.input[1] in constants_map:
                if len(constants_map[node.input[1]]) == 4:
                    shape_map[node.output[0]] = [constants_map[node.input[1]][0], constants_map[node.input[1]][2],
                                                 constants_map[node.input[1]][3], constants_map[node.input[1]][1]]
                else:
                    shape_map[node.output[0]] = constants_map[node.input[1]]

                result = np.zeros(shape_map[node.output[0]]) + constants_map[node.input[0]]
                constants_map[node.output[0]] = result
        else:
            assert 0, "Operations of type " + node.op_type + " are not yet supported."

    # print('const_map')
    # print(constants_map)
    # print('shape_map')
    # print(shape_map)
    return shape_map, constants_map, output_node_map, input_node_map, placeholdernames, BN_map


class ONNXTranslator:
    """
	This class is used to turn a ONNX model into two lists that then can be processed by an Optimizer object
	"""

    def __init__(self, model):
        """
		This constructor takes a reference to a ONNX Model and checks model, infers intermediate shapes and sets up maps from name to type and node or constant value
		graph_util.convert_variables_to_constants and graph_util.remove_training_nodes to cleanse the graph of any nodes that are linked to training. This leaves us with 
		the nodes you need for inference. 
		In the resulting graph there should only be tf.Operations left that have one of the following types [Const, MatMul, Add, BiasAdd, Conv2D, Reshape, MaxPool, AveragePool, Placeholder, Relu, Sigmoid, Tanh]
		If the input should be a Keras model we will ignore operations with type Pack, Shape, StridedSlice, and Prod such that the Flatten layer can be used.
		
		Arguments
		---------
		model : onnx.ModelProto
		"""
        if issubclass(model.__class__, onnx.ModelProto):
            onnx.checker.check_model(model)
            self.model = model
            self.nodes = self.model.graph.node
            self.shape_map, self.constants_map, self.output_node_map, self.input_node_map, self.placeholdernames, self.BN_map = prepare_model(model)
        else:
            assert 0, 'not onnx model'

    def translate(self):
        """
		The constructor has produced a graph_def with the help of the functions graph_util.convert_variables_to_constants and graph_util.remove_training_nodes.
		translate() takes that graph_def, imports it, and translates it into two lists which then can be processed by an Optimzer object.
		
		Return
		------
		(operation_types, operation_resources) : (list, list)
		    A tuple with two lists, the first one has items of type str and the second one of type dict. In the first list the operation types are stored (like "Add", "MatMul", etc.).
		    In the second list we store the resources (matrices, biases, etc.) for those operations. It is organised as follows: operation_resources[i][domain] has the resources related to
		    operation_types[i] when analyzed with domain (domain is currently either 'deepzono' or 'deeppoly', as of 8/30/18)
		"""
        operation_types = ["Placeholder"]
        placeholder = self.model.graph.input[0]
        in_out_placeholder = ([], placeholder.name, onnxshape_to_intlist(placeholder.type.tensor_type.shape))
        operation_resources = [{'deepzono': in_out_placeholder, 'deeppoly': in_out_placeholder}]
        reshape_map = {}
        #operations_to_be_ignored = ["Pack", "Shape", "StridedSlice", "Prod", "Concat", "Unsqueeze", "Softmax", "Flatten", "BatchNormalization"]
        operations_to_be_ignored = ["Pack", "Shape", "StridedSlice", "Prod", "Concat", "Unsqueeze", "Softmax","Flatten"]
        # print("nodes ", self.nodes, "placeholder ", self.model.graph.input[0])
        for node in self.nodes:
            if node.op_type == "Constant":
                continue
            elif node.op_type in operations_to_be_ignored:
                input_name = node.input[0]
                output_name = node.output[0]
                if input_name in reshape_map:
                    reshape_map[output_name] = reshape_map[input_name]
                else:
                    reshape_map[output_name] = input_name
                continue

            operation_types.append(node.op_type)
            # take means and stds out of the network
            if len(operation_types) == 2 and node.op_type in ["Add", "Sub", "Mul", "Div"] and node.output[
                0] not in self.constants_map:
                constant = self.add_resources(node)[0].reshape(-1)
                if node.op_type == "Add":
                    config.mean = np.multiply(constant, -1)
                elif node.op_type == "Sub":
                    config.mean = constant
                elif node.op_type == "Mul":
                    config.std = np.divide(1, constant)
                elif node.op_type == "Div":
                    config.std = constant

                self.ignore_node(node, operation_types, reshape_map)
                continue

            input_onnx_names = []
            for name in node.input:
                kind = self.get_kind(name)
                if name in reshape_map:
                    name = reshape_map[name]
                if kind == 'Constant':
                    continue
                input_onnx_names.append(name)
            shape = self.get_shape(node.output[0])
            #The in_out_info includes the input name, output name, output shape
            in_out_info = (input_onnx_names, node.output[0], shape)

            if node.op_type == "MatMul":
                deeppoly_res = self.matmul_resources(node) + in_out_info
                deepzono_res = deeppoly_res
                operation_resources.append({'deepzono': deepzono_res, 'deeppoly': deeppoly_res})
            elif node.op_type == "Gemm":
                deeppoly_res = self.gemm_resources(node) + in_out_info
                deepzono_res = deeppoly_res
                operation_resources.append({'deepzono': deepzono_res, 'deeppoly': deeppoly_res})
            elif node.op_type in ["Add", "Mul"]:
                left_type = self.get_kind(node.input[0])
                right_type = self.get_kind(node.input[1])
                if left_type == 'Constant' and right_type == 'Constant':
                    operation_types.pop()
                elif left_type == 'Constant' or right_type == 'Constant':
                    deeppoly_res = self.add_resources(node) + in_out_info
                    deepzono_res = deeppoly_res
                    operation_resources.append({'deepzono': deepzono_res, 'deeppoly': deeppoly_res})
                else:
                    if node.op_type != "Add":
                        assert 0, "we don't support residual operations other then add"
                    operation_types[-1] = "Resadd"
                    operation_resources.append({'deepzono': in_out_info, 'deeppoly': in_out_info})

            elif node.op_type == "Sub":
                left_type = self.get_kind(node.input[0])
                right_type = self.get_kind(node.input[1])
                if left_type == 'Constant' and right_type == 'Constant':
                    assert 0, "we don't support the subraction of two constants yet"
                elif left_type == 'Constant' or right_type == 'Constant':
                    deeppoly_res = self.sub_resources(node) + in_out_info
                    deepzono_res = deeppoly_res
                    operation_resources.append({'deepzono': deepzono_res, 'deeppoly': deeppoly_res})
                else:
                    assert 0, "we don't support the ressub yet"
                    operation_types[-1] = "Ressub"
                    operation_resources.append({'deepzono': in_out_info, 'deeppoly': in_out_info})
            elif node.op_type == "Conv":
                #change the value of filters and bias in here, then just let this parameter values be passed to the following analysis
                filters, bias, image_shape, strides, pad_top, pad_left, kernel_shape = self.conv_resources(node)
                deeppoly_res = (filters, bias, image_shape, strides, pad_top, pad_left) + in_out_info
                print ("Conv filter shape ",filters.shape)
                deepzono_res = deeppoly_res
                operation_resources.append({'deepzono': deepzono_res, 'deeppoly': deeppoly_res})
            elif node.op_type == "BatchNormalization":
                #Handle the BN layer in here
                inputs = node.input
                image = inputs[0]
                image_shape = self.get_shape(image)[1:]
                scale_para = self.constants_map[node.input[1]]
                bias_para = self.constants_map[node.input[2]]
                mean_para = self.constants_map[node.input[3]]
                var_para = self.constants_map[node.input[4]]
                for attribute in node.attribute:
                    if attribute.name == 'epsilon':
                        epsilon = attribute.f
                    elif attribute.name == 'momentum':
                        momentum = attribute.f
                deeppoly_res = (scale_para, bias_para, mean_para, var_para, epsilon, image_shape) + in_out_info
                #print ("deeppoly_res of BN layer ",deeppoly_res)
                deepzono_res = deeppoly_res
                operation_resources.append({'deepzono': deepzono_res, 'deeppoly': deeppoly_res})
            elif node.op_type == "MaxPool" or node.op_type == "AveragePool":
                image_shape, kernel_shape, strides, padding, dilations, pad_top, pad_left, ceil_mode, storage_order = self.pool_resources(
                    node)
                deeppoly_res = (image_shape, kernel_shape, strides, pad_top, pad_left) + in_out_info
                # TODO padding is expected to be string in tf. dilations, auto_pad, ceil_mode, storage_order are unused at the moment
                deepzono_res = (image_shape, kernel_shape, strides, pad_top, pad_left) + in_out_info
                operation_resources.append({'deepzono': deepzono_res, 'deeppoly': deeppoly_res})
            elif node.op_type == "Placeholder":
                assert 0, "Placeholder is not in the ONNX graph"
            elif node.op_type in ["Relu", "Sigmoid", "Tanh"]:
                deeppoly_res = self.nonlinearity_resources(node) + in_out_info
                deepzono_res = deeppoly_res
                operation_resources.append({'deepzono': deepzono_res, 'deeppoly': deeppoly_res})

            # Gather is for the moment solely for shapes
            elif node.op_type == "Gather":
                only_shape, image_shape, indexes, axis = self.gather_resources(node)
                if only_shape:
                    self.ignore_node(node, operation_types, reshape_map)
                else:
                    deeppoly_res = (image_shape, indexes, axis) + in_out_info
                    deepzono_res = deeppoly_res
                    operation_resources.append({'deepzono': deepzono_res, 'deeppoly': deeppoly_res})

            elif node.op_type == "Expand":
                only_shape, image_shape, to_expand = self.expand_resources(node)
                if only_shape:
                    operation_types.pop()
                else:
                    deeppoly_res = (image_shape, indexes, axis) + in_out_info
                    deepzono_res = deeppoly_res
                    operation_resources.append({'deepzono': deepzono_res, 'deeppoly': deeppoly_res})

            elif node.op_type == "Reshape":
                if node.output[0] in self.input_node_map and self.input_node_map[node.output[0]].op_type in ["MatMul",
                                                                                                             "Gemm"]:
                    self.ignore_node(node, operation_types, reshape_map)

                elif node.output[0] in self.input_node_map and self.input_node_map[node.output[0]].op_type in ["Relu",
                                                                                                               "Sigmoid",
                                                                                                               "Tanh"] and \
                        self.input_node_map[self.input_node_map[node.output[0]].output[0]].op_type == "Reshape":
                    # ignore this reshape even in the shape_map
                    self.shape_map[node.output[0]] = self.shape_map[node.input[0]]
                    self.shape_map[self.input_node_map[node.output[0]].output[0]] = self.shape_map[node.input[0]]
                    self.ignore_node(node, operation_types, reshape_map)
                else:
                    shape_in = self.get_shape(node.input[0])
                    shape_out = self.get_shape(node.output[0])
                    if len(shape_in) == 2 and len(shape_out) == 2:
                        self.ignore_node(node, operation_types, reshape_map)
                    else:
                        indexes = reshape_nhwc(shape_in, shape_out)
                        deeppoly_res = (indexes,) + in_out_info
                        deepzono_res = deeppoly_res
                        operation_resources.append({'deepzono': deepzono_res, 'deeppoly': deeppoly_res})
            else:
                assert 0, "Operations of type " + node.op_type + " are not yet supported."
        assert operation_types[0] == "Placeholder", "the optimizer for Deeppoly cannot handle this network "
        input_names, output_name, output_shape = operation_resources[0]['deeppoly']
        print(input_names, output_name, output_shape)
        return operation_types, operation_resources

    def ignore_node(self, node, operation_types, reshape_map):
        operation_types.pop()
        input_name = node.input[0]
        # print("ignore ", len(node.input), reshape_map)
        output_name = node.output[0]
        if input_name in reshape_map:
            reshape_map[output_name] = reshape_map[input_name]
        else:
            reshape_map[output_name] = input_name

    def get_kind(self, name):
        if name in self.constants_map:
            kind = 'Constant'
        elif name in self.placeholdernames:
            kind = 'Placeholder'
        else:
            kind = self.output_node_map[name].op_type
        return kind

    def get_shape(self, name):
        if name in self.shape_map:
            return self.shape_map[name]

    def matmul_resources(self, node):
        """
		checks which one of the direct ancestor tf.Operations is a constant and returns the underlying onnx as a numpy.ndarray inside a tuple. The matrix is manipulated in a way that it can be
		used as the left multiplier in the matrix multiplication.

		Arguments
		---------
		node : ONNX.Node
		    must have op_type "MatMul"

		Return
		------
		output : tuple
		    tuple with the matrix (of type numpy.ndarray) as its only item
		"""
        inputs = node.input
        left = inputs[0]
        right = inputs[1]

        if left in self.constants_map:
            matrix = self.constants_map[left]
            matrix = self.reshape_adjust(right, matrix, True)
        else:
            matrix = self.constants_map[right].transpose()
            matrix = self.reshape_adjust(left, matrix)
        return matrix,

    def reshape_adjust(self, element, matrix, is_right=False):
        if self.get_kind(element) == 'Reshape':
            shape_in = self.get_shape(self.output_node_map[element].input[0])
            shape_out = self.get_shape(self.output_node_map[element].output[0])
            if config.debug:
                print('reshape adjust ', str(shape_in), 'to', str(shape_out))
            indexes = reshape_nhwc(shape_in, shape_out)
            # indexes = indexes[0]
            inverse_perm = np.arange(len(indexes))[np.argsort(indexes)]
            if is_right:
                matrix = matrix[inverse_perm, :]
            else:
                matrix = matrix[:, inverse_perm]
        return matrix

    def gemm_resources(self, node):
        """
		checks which one of the direct ancestor tf.Operations is a constant and returns the underlying onnx as a numpy.ndarray inside a tuple. The matrix is manipulated in a way that it can be
		used as the left multiplier in the matrix multiplication.

		Arguments
		---------
		node : ONNX.Node
		    must have op_type "Gemm"

		Return
		------
		output : tuple
		    tuple with the matrix and bias (of type numpy.ndarray) and is_left used to calculate the output shape
		"""
        inputs = node.input
        left = inputs[0]
        right = inputs[1]
        bias = self.constants_map[inputs[2]]

        transA = False
        transB = False
        alpha = 1.0
        beta = 1.0
        for att in node.attribute:
            if 'transA' == att.name:
                transA = att.i == 1
            elif 'transB' == att.name:
                transB = att.i == 1
            elif 'alpha' == att.name:
                alpha = att.f
            elif 'beta' == att.name:
                beta = att.f
            else:
                assert 0, "Unkown attribute " + att.name + " for operation type " + node.op_type

        if left in self.constants_map:
            matrix = self.constants_map[left] if not transA else self.constants_map[left].transpose()
            matrix = self.reshape_adjust(right, matrix, True)
        else:
            matrix = self.constants_map[right].transpose() if not transB else self.constants_map[right]
            matrix = self.reshape_adjust(left, matrix)
        return matrix * alpha, bias * beta

    def add_resources(self, node):
        """
		checks which one of the direct ancestor tf.Operations is a constant and returns the underlying onnx as a numpy.ndarray inside a tuple.

		Arguments
		---------
		node : ONNX.Node
		    must have op_type "Add"

		Return
		------
		output : tuple
		    tuple with the addend (of type numpy.ndarray) as its only item
		"""
        inputs = node.input
        left = inputs[0]
        right = inputs[1]

        if left in self.constants_map:
            addend = self.constants_map[left]
        else:
            addend = self.constants_map[right]
        return addend,

    def sub_resources(self, node):
        """
		checks which one of the direct ancestors is a constant and returns the underlying onnx as a numpy.ndarray and a bool is_minuend, whether the returned ndarray is the minuend, inside a tuple.

		Arguments
		---------
		node : ONNX.Node
		    must have op_type "Sub"

		Return
		------
		output : tuple
		    tuple with the addend (of type numpy.ndarray) and left_constant
		"""
        inputs = node.input
        left = inputs[0]
        right = inputs[1]

        if left in self.constants_map:
            addend = self.constants_map[left]
            is_minuend = True
        else:
            addend = self.constants_map[right]
            is_minuend = False
        return addend, is_minuend

    def conv_resources(self, node):
        """
		Extracts the filter, the stride of the filter, and the padding from node as well as the shape of the input coming into node
		
		Arguments
		---------
		node : ONNX.Node
		    must have op_type "Conv"
		
		Return 
		------
		output : tuple
		    has 4 entries (numpy.ndarray, numpy.ndarray, numpy.ndarray, str)
		"""
        inputs = node.input
        image = inputs[0]
        filters = self.constants_map[node.input[1]].transpose(1, 2, 3, 0)
        if len(node.input) == 3:
            bias = self.constants_map[node.input[2]]
        else:
            bias = np.zeros(filters.shape[3])
        image_shape = self.get_shape(image)[1:]
        pads = [0, 0, 0, 0]
        for attribute in node.attribute:
            if attribute.name == 'strides':
                strides = attribute.ints
            elif attribute.name == 'pads':
                pads = attribute.ints
            elif attribute.name == 'kernel_shape':
                kernel_shape = attribute.ints

        pad_top = pads[0]
        pad_left = pads[1]
        pad_bottom = pads[2]
        pad_right = pads[3]
        assert pad_top == pad_bottom, 'different padding for top and bottom is not supported in ERAN'
        assert pad_left == pad_right, 'different padding for left and right is not supported in ERAN'
        return filters, bias, image_shape, strides, pad_top, pad_left, kernel_shape

    def conca_conv_BN(self, node, filters, bias):
        #print(bias)
        convou_BNin = node.output[0]
        BN_node = self.BN_map[convou_BNin]
        input_x = BN_node.input[0]
        assert convou_BNin == input_x, "The output of Conv doesn't match with input to BatchNorm layer!"
        input_x_shape = self.shape_map[BN_node.input[0]]
        #print("filter shape ", filters.shape)
        assert filters.shape[2]==input_x_shape[3] and filters.shape[3]==self.shape_map[BN_node.input[1]][0], "The number of channels should be the same"
        scale_para = self.constants_map[BN_node.input[1]]
        bias_para = self.constants_map[BN_node.input[2]]
        mean_para = self.constants_map[BN_node.input[3]]
        var_para = self.constants_map[BN_node.input[4]]
        for attribute in BN_node.attribute:
            if attribute.name == 'epsilon':
                epsilon = attribute.f
            elif attribute.name == 'momentum':
                momentum = attribute.f
        for i in range(filters.shape[2]):
            # handle the concatenation of conv and BN for each channel
            scale = scale_para[i]
            BN_bias = bias_para[i]
            mean = mean_para[i]
            var = var_para[i]
            cons_bias = BN_bias - ((scale*mean)/math.sqrt(var+epsilon))
            cons_wei = scale/math.sqrt(var+epsilon)
            bias[i]=cons_bias+bias[i]
            new_filter = filters[:, :, :, i] * cons_wei
            filters[:, :, :, i] = new_filter
        return filters, bias

    def pool_resources(self, node):
        """
		Extracts the incoming image size (heigth, width, channels), the size of the maxpool/averagepool window (heigth, width), and the strides of the window (heigth, width)
		
		Arguments
		---------
		node : ONNX.Node
		    must have op_type "MaxPool" or "AveragePool"
		
		Return
		------
		output : tuple
		    has 4 entries - (list, numpy.ndarray, numpy.ndarray, numpy.ndarray, int, int, str)
		"""
        image = node.input[0]

        image_shape = self.get_shape(image)[1:]

        padding = 'NOTSET'
        ceil_mode = 0
        storage_order = 0
        pads = [0, 0, 0, 0]
        dilations = None

        for attribute in node.attribute:
            if attribute.name == 'kernel_shape':
                kernel_shape = attribute.ints
            if attribute.name == 'strides':
                strides = attribute.ints
            elif attribute.name == 'pads':
                pads = attribute.ints
            elif attribute.name == 'dilations':
                dilations = attribute.ints
            elif attribute.name == 'auto_pad':
                padding = attribute.s
            elif attribute.name == 'ceil_mode':
                ceil_mode = attribute.i
            elif attribute.name == 'storage_order':
                storage_order = attribute.i
        pad_top = pads[0]
        pad_left = pads[1]
        pad_bottom = pads[2]
        pad_right = pads[3]
        assert pad_top == pad_bottom, 'different padding for top and bottom is not supported in ERAN'
        assert pad_left == pad_right, 'different padding for left and right is not supported in ERAN'
        return image_shape, kernel_shape, strides, padding, dilations, pad_top, pad_left, ceil_mode, storage_order

    def nonlinearity_resources(self, op):
        """
		This function only outputs an empty tuple, to make the code look more consistent
		
		Return
		------
		output : tuple
		    but is empty
		"""
        return ()

    def gather_resources(self, node):
        """
		Extracts the indexes in the image which have to be gathered.

		Arguments
		---------
		node : ONNX.Node
		    must have op_type "Gather"

		Return
		------
		output : tuple
		    has 4 entries - (list, numpy.ndarray, numpy.ndarray, numpy.ndarray, int, int, str)
		"""
        inputs = node.input
        image = inputs[0]
        if node.output[0] in self.constants_map:
            only_shape = True
            image_shape, indexes, axis = None, None, None
        else:
            only_shape = False
            image_shape = self.get_shape(image)[1:]
            indexes = self.constants_map[node.input[1]]
            axis = node.attribute[0].i
        return only_shape, image_shape, indexes, axis

    def expand_resources(self, node):
        if node.output[0] in self.constants_map:
            only_shape = True
            image_shape, to_expand = None, None
        else:
            assert 0, "Implementation for 'Expand' is missing."
        return only_shape, image_shape, to_expand


if __name__ == '__main__':
    '''
    test = np.random.rand(1,2,3,4)
    print ("test", test)
    test = test.transpose(3, 0, 1, 2)
    one_di = test[1, :,:,:]
    print ("one_di",one_di)
    one_di = one_di *2
    test[1, :,:,:] = one_di
    test = test.transpose(1, 2, 3, 0)
    print ("final test", test)
    '''
    onnx_model = onnx.load("/Users/zhongyuyi/Desktop/onnx models/red_dncnn_mnist.onnx")
    onnx.checker.check_model(onnx_model)
    is_conv = False
    for node in onnx_model.graph.node:
        if node.op_type == 'Conv':
            is_conv = True
            break
    translator = ONNXTranslator(onnx_model)
    operations, resources = translator.translate()