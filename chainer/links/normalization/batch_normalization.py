import numpy

from chainer.backends import cuda
from chainer import configuration
from chainer import functions
from chainer import initializers
from chainer import link
from chainer.utils import argument
from chainer import variable


class BatchNormalization(link.Link):

    """Batch normalization layer on outputs of linear or convolution functions.

    This link wraps the :func:`~chainer.functions.batch_normalization` and
    :func:`~chainer.functions.fixed_batch_normalization` functions.

    It runs in three modes: training mode, fine-tuning mode, and testing mode.

    In training mode, it normalizes the input by *batch statistics*. It also
    maintains approximated population statistics by moving averages, which can
    be used for instant evaluation in testing mode.

    In fine-tuning mode, it accumulates the input to compute *population
    statistics*. In order to correctly compute the population statistics, a
    user must use this mode to feed mini-batches running through whole training
    dataset.

    In testing mode, it uses pre-computed population statistics to normalize
    the input variable. The population statistics is approximated if it is
    computed by training mode, or accurate if it is correctly computed by
    fine-tuning mode.

    Args:
        size (int, tuple of ints, or None): Size (or shape) of channel
            dimensions.  If ``None``, the size will be determined from
            dimension(s) of the input batch during the first forward pass.
        decay (float): Decay rate of moving average. It is used on training.
        eps (float): Epsilon value for numerical stability.
        dtype (numpy.dtype): Type to use in computing.
        use_gamma (bool): If ``True``, use scaling parameter. Otherwise, use
            unit(1) which makes no effect.
        use_beta (bool): If ``True``, use shifting parameter. Otherwise, use
            unit(0) which makes no effect.

    See: `Batch Normalization: Accelerating Deep Network Training by Reducing\
          Internal Covariate Shift <https://arxiv.org/abs/1502.03167>`_

    .. seealso::
       :func:`~chainer.functions.batch_normalization`,
       :func:`~chainer.functions.fixed_batch_normalization`

    Attributes:
        gamma (~chainer.Variable): Scaling parameter.
        beta (~chainer.Variable): Shifting parameter.
        avg_mean (numpy.ndarray or cupy.ndarray): Population mean.
        avg_var (numpy.ndarray or cupy.ndarray): Population variance.
        N (int): Count of batches given for fine-tuning.
        decay (float): Decay rate of moving average. It is used on training.
        ~BatchNormalization.eps (float): Epsilon value for numerical stability.
            This value is added to the batch variances.
        axis (int or tuple of int): Axis over which normalization is
            performed. When axis is ``None``, it is determined from input
            dimensions. For example, if ``x.ndim`` is 4, axis becomes (0, 2, 3)
            and normalization is performed over 0th, 2nd and 3rd axis of input.
            If it is 2, axis becomes (0) and normalization is performed
            over 0th axis of input. When a tuple of int is given to this
            option, numbers in the tuple must be being sorted in ascending
            order. For example, (0, 2) is OK, but (2, 0) is not.

    .. admonition:: Example

        >>> x = np.arange(12).reshape(4, 3).astype(np.float32) ** 2
        >>> x
        array([[  0.,   1.,   4.],
               [  9.,  16.,  25.],
               [ 36.,  49.,  64.],
               [ 81., 100., 121.]], dtype=float32)
        >>> bn = chainer.links.BatchNormalization(3)
        >>> bn(x)
        variable([[-1.        , -1.0664359 , -1.1117983 ],
                  [-0.71428573, -0.6714596 , -0.6401263 ],
                  [ 0.14285715,  0.19748813,  0.23583598],
                  [ 1.5714287 ,  1.5404074 ,  1.5160885 ]])

        There are several ways to make a BatchNormalization link.
        Consider an input of batched 10 images of 32x32 with 3 channels.

        >>> x = np.ones((10, 3, 32, 32), np.float32)

        1. Give the parameter size:

            To normalize for each channel, give the number of channels
            to ``size``.

            >>> bn = chainer.links.BatchNormalization(3)
            >>> bn.avg_mean.shape
            (3,)
            >>> y = bn(x)
            >>> y.shape
            (10, 3, 32, 32)

            To normalize for each channel for each pixel, ``size`` should
            be the tuple of the dimensions.

            >>> bn = chainer.links.BatchNormalization((3, 32, 32))
            >>> bn.avg_mean.shape
            (3, 32, 32)

            By default, channel axis is (or starts from) the 1st axis of the
            input shape.

        2. Give the aggregate axes:

            from Chainer v5

            With ``axis`` option, similarly to NumPy, you may specify the
            aggregate axes, which are treated as the "batch" axes for the
            batch statistics.
            The examples in 1. corresponds to the following, respectively.

            >>> bn = chainer.links.BatchNormalization(axis=(0, 2, 3))
            >>> y = bn(x)
            >>> bn.avg_mean.shape
            (3,)

            >>> bn = chainer.links.BatchNormalization(axis=0)
            >>> y = bn(x)
            >>> bn.avg_mean.shape
            (3, 32, 32)

            You can omit ``size`` if ``axis`` is given.

    """

    def __init__(self, size=None, decay=0.9, eps=2e-5, dtype=numpy.float32,
                 use_gamma=True, use_beta=True,
                 initial_gamma=None, initial_beta=None, axis=None):
        super(BatchNormalization, self).__init__()

        if size is None and axis is None:
            raise RuntimeError('size or axis is required')
        self.N = 0
        self.register_persistent('N')
        self.decay = decay
        self.eps = eps
        if isinstance(axis, int):
            axis = (axis,)
        self.axis = axis
        self._dtype = dtype

        with self.init_scope():
            if use_gamma:
                if initial_gamma is None:
                    initial_gamma = 1
                gamma_initializer = \
                    initializers._get_initializer(initial_gamma)
                gamma_initializer.dtype = self._dtype
                self.gamma = variable.Parameter(gamma_initializer)
            if use_beta:
                if initial_beta is None:
                    initial_beta = 0
                beta_initializer = initializers._get_initializer(initial_beta)
                beta_initializer.dtype = self._dtype
                self.beta = variable.Parameter(beta_initializer)

        if size is not None:
            self._initialize_params(size)

    def _initialize_params(self, shape):
        self.avg_mean = numpy.zeros(shape, dtype=self._dtype)
        self.register_persistent('avg_mean')
        self.avg_var = numpy.zeros(shape, dtype=self._dtype)
        self.register_persistent('avg_var')
        if hasattr(self, 'gamma'):
            self.gamma.initialize(shape)
        if hasattr(self, 'beta'):
            self.beta.initialize(shape)

    def __call__(self, x, **kwargs):
        """__call__(self, x, finetune=False)

        Invokes the forward propagation of BatchNormalization.

        In training mode, the BatchNormalization computes moving averages of
        mean and variance for evaluation during training, and normalizes the
        input using batch statistics.

        .. warning::

           ``test`` argument is not supported anymore since v2.
           Instead, use ``chainer.using_config('train', False)``.
           See :func:`chainer.using_config`.

        Args:
            x (Variable): Input variable.
            finetune (bool): If it is in the training mode and ``finetune`` is
                ``True``, BatchNormalization runs in fine-tuning mode; it
                accumulates the input array to compute population statistics
                for normalization, and normalizes the input using batch
                statistics.

        """
        if not hasattr(self, 'avg_mean'):
            param_shape = tuple([
                d
                for i, d in enumerate(x.shape)
                if i not in self.axis])
            self._initialize_params(param_shape)

        argument.check_unexpected_kwargs(
            kwargs, test='test argument is not supported anymore. '
            'Use chainer.using_config')
        finetune, = argument.parse_kwargs(kwargs, ('finetune', False))

        if hasattr(self, 'gamma'):
            gamma = self.gamma
        else:
            with cuda.get_device_from_id(self._device_id):
                gamma = variable.Variable(self.xp.ones(
                    self.avg_mean.shape, dtype=x.dtype))

        if hasattr(self, 'beta'):
            beta = self.beta
        else:
            with cuda.get_device_from_id(self._device_id):
                beta = variable.Variable(self.xp.zeros(
                    self.avg_mean.shape, dtype=x.dtype))

        if configuration.config.train:
            if finetune:
                self.N += 1
                decay = 1. - 1. / self.N
            else:
                decay = self.decay

            ret = functions.batch_normalization(
                x, gamma, beta, eps=self.eps, running_mean=self.avg_mean,
                running_var=self.avg_var, decay=decay, axis=self.axis)
        else:
            # Use running average statistics or fine-tuned statistics.
            mean = variable.Variable(self.avg_mean)
            var = variable.Variable(self.avg_var)
            ret = functions.fixed_batch_normalization(
                x, gamma, beta, mean, var, self.eps, axis=self.axis)
        return ret

    def start_finetuning(self):
        """Resets the population count for collecting population statistics.

        This method can be skipped if it is the first time to use the
        fine-tuning mode. Otherwise, this method should be called before
        starting the fine-tuning mode again.

        """
        self.N = 0
