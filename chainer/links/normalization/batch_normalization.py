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
        size (int, tuples of ints or None): Size (or shape) of channel
            dimensions. If ``None``, the size will be determined from an axis
            or axes of channel dimensions given by ``axis`` during the first
            forward pass.
        decay (float): Decay rate of moving average. It is used on training.
        eps (float): Epsilon value for numerical stability.
        dtype (numpy.dtype): Type to use in computing.
        use_gamma (bool): If ``True``, use scaling parameter. Otherwise, use
            unit(1) which makes no effect.
        use_beta (bool): If ``True``, use shifting parameter. Otherwise, use
            unit(0) which makes no effect.
        axis (int or tuple of ints): Axis or axes of channel dimension(s). When
             ``size``` is ``None``, this value is used to determine parameter
             sizes during the first forward pass. Otherwise, this value is
             ignored.

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

    """

    def __init__(self, size=None, decay=0.9, eps=2e-5,
                 dtype=numpy.float32, use_gamma=True, use_beta=True,
                 initial_gamma=None, initial_beta=None, axis=1):
        super(BatchNormalization, self).__init__()

        self.N = 0
        self.register_persistent('N')
        self.decay = decay
        self.eps = eps

        self._size = size
        self._axis = (axis,) if isinstance(axis, int) else tuple(axis)
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
        if self.gamma.data is None:
            if self._size is not None:
                self._initialize_params(self._size)
            else:
                shape = tuple(x.shape[i] for i in self._axis)
                self._initialize_params(shape)

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
                running_var=self.avg_var, decay=decay)
        else:
            # Use running average statistics or fine-tuned statistics.
            mean = variable.Variable(self.avg_mean)
            var = variable.Variable(self.avg_var)
            ret = functions.fixed_batch_normalization(
                x, gamma, beta, mean, var, self.eps)
        return ret

    def start_finetuning(self):
        """Resets the population count for collecting population statistics.

        This method can be skipped if it is the first time to use the
        fine-tuning mode. Otherwise, this method should be called before
        starting the fine-tuning mode again.

        """
        self.N = 0
