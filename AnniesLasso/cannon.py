#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
A pedestrian version of The Cannon.
"""

from __future__ import (division, print_function, absolute_import,
                        unicode_literals)

__all__ = ["CannonModel"]

import logging
import numpy as np
import scipy.optimize as op

from . import (model, utils)

logger = logging.getLogger(__name__)

design_matrix = None

class CannonModel(model.BaseCannonModel):
    """
    A generalised Cannon model for the estimation of arbitrary stellar labels.

    :param labelled_set:
        A set of labelled objects. The most common input form is a table with
        columns as labels, and stars/objects as rows.

    :type labelled_set:
        :class:`~astropy.table.Table`, numpy structured array

    :param normalized_flux:
        An array of normalized fluxes for stars in the labelled set, given as
        shape `(num_stars, num_pixels)`. The `num_stars` should match the number
        of rows in `labelled_set`.

    :type normalized_flux:
        :class:`np.ndarray`

    :param normalized_ivar:
        An array of inverse variances on the normalized fluxes for stars in the
        labelled set. The shape of the `normalized_ivar` array should match that
        of `normalized_flux`.

    :type normalized_ivar:
        :class:`np.ndarray`

    :param dispersion: [optional]
        The dispersion values corresponding to the given pixels. If provided, 
        this should have length `num_pixels`.

    :param threads: [optional]
        Specify the number of parallel threads to use. If `threads > 1`, the
        training and prediction phases will be automagically parallelised.

    :param pool: [optional]
        Specify an optional multiprocessing pool to map jobs onto.
        This argument is only used if specified and if `threads > 1`.
    """
    def __init__(self, *args, **kwargs):
        super(CannonModel, self).__init__(*args, **kwargs)


    @model.requires_model_description
    def train(self, fixed_scatter=False, progressbar=True, **kwargs):
        """
        Train the model based on the labelled set using the given vectorizer.

        :param fixed_scatter: [optional]
            Fix the scatter terms and do not solve for them during the training
            phase. If set to `True`, the `s2` attribute must be already set.

        :param progressbar: [optional]
            Show a progress bar.
        """
        
        if fixed_scatter and self.s2 is None:
            raise ValueError("intrinsic pixel variance (s2) must be set "
                             "before training if fixed_scatter is set to True")

        # Initialize the scatter.
        p0_scatter = np.sqrt(self.s2) if fixed_scatter \
            else 0.01 * np.ones_like(self.dispersion)

        # Prepare details about any progressbar to show.
        M, N = self.normalized_flux.shape
        message = None if not progressbar else \
            "Training {0} with {1} stars and {2} pixels/star".format(
                type(self).__name__, M, N)

        # Prepare the method and arguments.
        fitter = kwargs.pop("function", _fit_pixel)
        args = [self.normalized_flux.T, self.normalized_ivar.T, p0_scatter]
        args.extend(kwargs.pop("additional_args", []))

        kwds = {
            "fixed_scatter": fixed_scatter,
        #    "design_matrix": self.design_matrix
        }
        kwds.update(kwargs)
        """
        if self.pool is not None:

            # Sometimes the design matrix is *huge*, so we need to store it as
            # a shared memory array to prevent the multiprocessing pool from
            # completely hanging.

            def _init(dm):
                global design_matrix
                design_matrix = dm

            dm = self.design_matrix.flatten()
            _ = np.ctypeslib.as_ctypes(dm)
            shared_design_matrix \
                = mp.sharedctypes.Array(_._type_, _, lock=False)
            shared_design_matrix[:] = dm.copy()
            pool = self.pool.__class__(self.pool._processes,
                initializer=_init, initargs=(shared_design_matrix, ))
            self.pool.close()
            self.pool = pool
            mapper = self.pool.map

        else:
            mapper = map
            kwds["design_matrix"] = self.design_matrix
        """
        def _init(dm):
            global design_matrix
            design_matrix = dm

        global design_matrix
        design_matrix = self.design_matrix.copy()
        
        if self.pool is not None:
            self.pool.close()
            self.pool = self.pool.__class__(self.pool._processes,
                initializer=_init, initargs=(self.design_matrix, ))

        
        # Wrap the function so we can parallelize it out.
        mapper = map if self.pool is None else self.pool.map
        f = utils.wrapper(fitter, None, kwds, N, message=message)

        # Time for work.
        results = np.array(mapper(f, [row for row in zip(*args)]))
        
        # Unpack the results.
        self.theta, self.s2 = (results[:, :-1], results[:, -1]**2)
        return None


    @model.requires_training_wheels
    def predict(self, labels, **kwargs):
        """
        Predict spectra from the trained model, given the labels.

        :param labels:
            The label values to predict model spectra of. The length and order
            should match what is required of the vectorizer
            (`CannonModel.vectorizer.label_names`).
        """
        return np.dot(self.theta, self.vectorizer(labels).T).T


    @model.requires_training_wheels
    def fit(self, normalized_flux, normalized_ivar, **kwargs):
        """
        Solve the labels for the given normalized fluxes and inverse variances.

        :param normalized_flux:
            The normalized fluxes. These should be on the same dispersion scale
            as the trained data.

        :param normalized_ivar:
            The inverse variances of the normalized flux values. This should
            have the same shape as `normalized_flux`.

        :returns:
            The labels.
        """
        normalized_flux = np.atleast_2d(normalized_flux)
        normalized_ivar = np.atleast_2d(normalized_ivar)

        # Prepare the wrapper function and data.
        N_spectra = normalized_flux.shape[0]
        message = None if not kwargs.pop("progressbar", True) \
            else "Fitting {0} spectra".format(N_spectra)
        kwds = {
            "vectorizer": self.vectorizer,
            "theta": self.theta,
            "s2": self.s2
        }
        args = [normalized_flux, normalized_ivar]
        
        f = utils.wrapper(_fit_spectrum, None, kwds, N_spectra, message=message)

        # Do the grunt work.
        mapper = map if self.pool is None else self.pool.map
        labels, cov = map(np.array, zip(*mapper(f, [r for r in zip(*args)])))

        return (labels, cov) if kwargs.get("full_output", False) else labels


def _estimate_label_vector(theta, s2, normalized_flux, normalized_ivar,
    **kwargs):
    """
    Perform a matrix inversion to estimate the values of the label vector given
    some normalized fluxes and associated inverse variances.

    :param theta:
        The theta coefficients obtained from the training phase.

    :param s2:
        The intrinsic pixel variance.

    :param normalized_flux:
        The normalized flux values. These should be on the same dispersion scale
        as the labelled data set.

    :param normalized_ivar:
        The inverse variance of the normalized flux values. This should have the
        same shape as `normalized_flux`.
    """

    inv_var = normalized_ivar/(1. + normalized_ivar * s2)
    A = np.dot(theta.T, inv_var[:, None] * theta)
    B = np.dot(theta.T, inv_var * normalized_flux)
    return np.linalg.solve(A, B)


def _fit_spectrum(normalized_flux, normalized_ivar, vectorizer, theta, s2,
    **kwargs):
    """
    Solve the labels for given pixel fluxes and uncertainties for a single star.

    :param normalized_flux:
        The normalized fluxes. These should be on the same dispersion scale
        as the trained data.

    :param normalized_ivar:
        The 1-sigma uncertainties in the fluxes. This should have the same
        shape as `normalized_flux`.

    :param vectorizer:
        The model vectorizer.

    :param theta:
        The theta coefficients obtained from the training phase.

    :param s2:
        The intrinsic pixel variance.

    :returns:
        The labels and covariance matrix.
    """

    """
    # TODO: Re-visit this.
    # Get an initial estimate of the label vector from a matrix inversion,
    # and then ask the vectorizer to interpret that label vector into the 
    # (approximate) values of the labels that could have produced that 
    # label vector.
    lv = _estimate_label_vector(theta, s2, normalized_flux, normalized_ivar)
    initial = vectorizer.get_approximate_labels(lv)
    """

    # Overlook the bad pixels.
    inv_var = normalized_ivar/(1. + normalized_ivar * s2)
    use = np.isfinite(inv_var * normalized_flux)

    kwds = {
        "p0": vectorizer.fiducials,
        "maxfev": 10**6,
        "sigma": np.sqrt(1.0/inv_var[use]),
        "absolute_sigma": True
    }
    kwds.update(kwargs)
    
    f = lambda t, *l: np.dot(t, vectorizer(l).T).flatten()
    labels, cov = op.curve_fit(f, theta[use], normalized_flux[use], **kwds)
    return (labels, cov)


def _get_design_matrix(N, **kwargs):

    try:
        _ = kwargs.pop("design_matrix")
        return (_, kwargs)
    except KeyError:
        global design_matrix
        try:
            design_matrix.ndim
        except AttributeError:
            design_matrix = np.ctypeslib.as_array(design_matrix).reshape((N, -1))

    return (design_matrix, kwargs)

def _fit_pixel(normalized_flux, normalized_ivar, scatter, fixed_scatter=False, **kwargs):
    """
    Return the optimal vectorizer coefficients and variance term for a pixel
    given the normalized flux, the normalized inverse variance, and the design
    matrix.

    :param normalized_flux:
        The normalized flux values for a given pixel, from all stars.

    :param normalized_ivar:
        The inverse variance of the normalized flux values for a given pixel,
        from all stars.

    :param design_matrix:
        The design matrix for the spectral model.

    :param scatter:
        Fit the data using a fixed scatter term. If this value is set to None,
        then the scatter will be calculated.

    :returns:
        The optimised label vector coefficients and scatter for this pixel, even
        if it was supplied by the user.
    """

    #design_matrix, kwargs = _get_design_matrix(normalized_flux.shape[0], **kwargs)
            
    theta, ATCiAinv, inv_var, design_matrix = _fit_theta(normalized_flux, normalized_ivar,
        scatter)

    # Singular matrix or fixed scatter?
    if ATCiAinv is None or fixed_scatter:
        return np.hstack([theta, scatter if fixed_scatter else 0.0])

    # Optimise the pixel scatter, and at each pixel scatter value we will 
    # calculate the optimal vector coefficients for that pixel scatter value.
    op_scatter, fopt, direc, n_iter, n_funcs, warnflag = op.fmin_powell(
        _fit_pixel_with_fixed_scatter, scatter,
        args=(normalized_flux, normalized_ivar, design_matrix),
        maxiter=np.inf, maxfun=np.inf, disp=False, full_output=True)

    if warnflag > 0:
        logger.warning("Warning: {}".format([
            "Maximum number of function evaluations made during optimisation.",
            "Maximum number of iterations made during optimisation."
            ][warnflag - 1]))

    theta, ATCiAinv, inv_var, _ = _fit_theta(normalized_flux, normalized_ivar,
        op_scatter)
    return np.hstack([theta, op_scatter])


def _fit_pixel_with_fixed_scatter(scatter, normalized_flux, normalized_ivar,
    design_matrix, **kwargs):
    """
    Fit the normalized flux for a single pixel (across many stars) given some
    pixel variance term, and return the best-fit theta coefficients.

    :param scatter:
        The additional scatter to adopt in the pixel.

    :param normalized_flux:
        The normalized flux values for a single pixel across many stars.

    :param normalized_ivar:
        The inverse variance of the normalized flux values for a single pixel
        across many stars.

    :param design_matrix:
        The design matrix for the model.
    """

    theta, ATCiAinv, inv_var, design_matrix = _fit_theta(normalized_flux, normalized_ivar,
        scatter)

    return_theta = kwargs.get("__return_theta", False)
    if ATCiAinv is None:
        return 0.0 if not return_theta else (0.0, theta)

    # We take inv_var back from _fit_theta because it is the same quantity we 
    # need to calculate, and it saves us one operation.
    Q   = model._chi_sq(theta, design_matrix, normalized_flux, inv_var) \
        + model._log_det(inv_var)
    return (Q, theta) if return_theta else Q


def _fit_theta(normalized_flux, normalized_ivar, scatter):
    """
    Fit theta coefficients to a set of normalized fluxes for a single pixel.

    :param normalized_flux:
        The normalized fluxes for a single pixel (across many stars).

    :param normalized_ivar:
        The inverse variance of the normalized flux values for a single pixel
        across many stars.

    :param scatter:
        The additional scatter to adopt in the pixel.

    :param design_matrix:
        The model design matrix.

    :returns:
        The label vector coefficients for the pixel, the inverse variance matrix
        and the total inverse variance.
    """

    global design_matrix
    ivar = normalized_ivar/(1. + normalized_ivar * scatter**2)
    CiA = design_matrix * np.tile(ivar, (design_matrix.shape[1], 1)).T
    try:
        ATCiAinv = np.linalg.inv(np.dot(design_matrix.T, CiA))
    except np.linalg.linalg.LinAlgError:
        #if logger.getEffectiveLevel() == logging.DEBUG: raise
        return (np.hstack([1, [0] * (design_matrix.shape[1] - 1)]), None, ivar,
            design_matrix)

    ATY = np.dot(design_matrix.T, normalized_flux * ivar)
    theta = np.dot(ATCiAinv, ATY)

    return (theta, ATCiAinv, ivar, design_matrix)

