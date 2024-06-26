import collections
import datetime

import numpy as np

__all__ = ["TrainingStats", "Time"]


class SmoothedValue(object):
    """Track a series of values and provide access to smoothed values over a
    window or the global series average.
    """

    def __init__(self, window_size):
        self.deque = collections.deque(maxlen=window_size)

    def add_value(self, value):
        self.deque.append(value)

    def get_median_value(self):
        return np.median(self.deque)


def Time():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")


class TrainingStats(object):
    def __init__(self, window_size, stats_keys):
        self.window_size = window_size
        self.smoothed_losses_and_metrics = {key: SmoothedValue(window_size) for key in stats_keys}

    def update(self, stats):
        for k, v in stats.items():
            if k not in self.smoothed_losses_and_metrics:
                self.smoothed_losses_and_metrics[k] = SmoothedValue(self.window_size)
            self.smoothed_losses_and_metrics[k].add_value(v)

    def get(self, extras=None):
        stats = collections.OrderedDict()
        if extras:
            for k, v in extras.items():
                stats[k] = v
        for k, v in self.smoothed_losses_and_metrics.items():
            stats[k] = round(v.get_median_value(), 6)

        return stats

    def log(self, extras=None):
        d = self.get(extras)
        strs = []
        for k, v in d.items():
            strs.append("{}: {:x<6f}".format(k, v))
        strs = ", ".join(strs)
        return strs
