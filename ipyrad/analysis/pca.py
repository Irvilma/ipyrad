#!/usr/bin/env python

""" Scikit-learn principal componenents analysis for missing data """

from __future__ import print_function, division

import os
import sys
import itertools
import numpy as np
import pandas as pd

# ipyrad tools
from .snps_extracter import SNPsExtracter
from .snps_imputer import SNPsImputer
from ipyrad.analysis.utils import jsubsample_snps
from ipyrad.assemble.utils import IPyradError

# missing imports to be raised on class init
try:
    import toyplot
except ImportError:
    pass

_MISSING_TOYPLOT = ImportError("""
This ipyrad tool requires the plotting library toyplot. 
You can install it with the following command in a terminal.

conda install toyplot -c eaton-lab 
""")

try:
    from sklearn import decomposition 
    from sklearn.cluster import KMeans
    from sklearn.manifold import TSNE
    from sklearn.linear_model import LinearRegression
    from sklearn.neighbors import NearestCentroid   
except ImportError:
    pass

_MISSING_SKLEARN = """
This ipyrad tool requires the library scikit-learn.
You can install it with the following command in a terminal.

conda install scikit-learn -c conda-forge 
"""


# TODO: could allow LDA as alternative to PCA for supervised (labels) dsets.
# TODO: remove biallel singletons... (option, not sure it's a great idea...)

class PCA(object):
    """
    Principal components analysis of RAD-seq SNPs with iterative
    imputation of missing data.

    Parameters:
    -----------
    data: (str, several options)
        A general .vcf file or a .snps.hdf5 file produced by ipyrad.
    workdir: (str; default="./analysis-pca")
        A directory for output files. Will be created if absent.
    imap: (dict; default=None)
        Dictionary mapping population names to a list of sample names.
    minmap: (dict; default={})
        Dictionary mapping population names to float values (X).
        If a site does not have data across X proportion of samples for
        each population, respectively, the site is filtered from the data set.
    mincov: (float; default=0.5)
        If a site does not have data across this proportion of total samples
        in the data then it is filtered from the data set.
    impute_method: (str; default='sample')
        None, "sample", or an integer for the number of kmeans clusters.
    topcov: (float; default=0.9)
        Affects kmeans method only.    
        The most stringent mincov used as the first iteration in kmeans 
        clustering. Subsequent iterations (niters) are equally spaced between
        topcov and mincov. 
    niters: (int; default=5)
        Affects kmeans method only.        
        kmeans method only.
        Number of iterations of kmeans clustering with decreasing mincov 
        thresholds used to refine population clustering, and therefore to 
        refine the imap groupings used to filter and impute sites.

    Functions:
    ----------
    ...
    """
    def __init__(
        self, 
        data, 
        impute_method=None,
        imap=None,
        minmap=None,
        mincov=0.1,
        quiet=False,
        topcov=0.9,
        niters=5,
        #ncomponents=None,
        ):

        # only check import at init
        if not sys.modules.get("sklearn"):
            raise IPyradError(_MISSING_SKLEARN)
        if not sys.modules.get("toyplot"):
            raise IPyradError(_MISSING_TOYPLOT)

        # init attributes
        self.quiet = quiet
        self.data = os.path.realpath(os.path.expanduser(data))

        # data attributes
        self.impute_method = impute_method
        self.mincov = mincov        
        self.imap = (imap if imap else {})
        self.minmap = (minmap if minmap else {i: 1 for i in self.imap})
        self.topcov = topcov
        self.niters = niters

        # where the resulting data are stored.
        self.pcaxes = "No results, you must first call .run()"
        self.variances = "No results, you must first call .run()"

        # to be filled
        self.snps = np.array([])
        self.snpsmap = np.array([])
        self.nmissing = 0

        # coming soon...
        if self.data.endswith(".vcf"):
            raise NotImplementedError(
                "Sorry, not yet supported. Use .snps.hdf5.")

        # load .snps and .snpsmap from HDF5
        first = (True if isinstance(self.impute_method, int) else quiet)
        ext = SNPsExtracter(
            self.data, self.imap, self.minmap, self.mincov, quiet=first,
        )

        # run snp extracter to parse data files
        ext.parse_genos_from_hdf5()       
        self.snps = ext.snps
        self.snpsmap = ext.snpsmap
        self.names = ext.names
        self._mvals = ext._mvals

        # make imap for imputing if not used in filtering.
        if not self.imap:
            self.imap = {'1': self.names}
            self.minmap = {'1': 0.5}

        # record missing data per sample
        self.missing = pd.DataFrame({
            "missing": [0.],
            },
            index=self.names,
        )
        miss = np.sum(self.snps == 9, axis=1) / self.snps.shape[1]
        for name in self.names:
            self.missing.missing[name] = round(miss[self.names.index(name)], 2)

        # impute missing data
        if (self.impute_method is not False) and self._mvals:
            self._impute_data()


    def _seed(self):   
        return np.random.randint(0, 1e9)        


    def _print(self, msg):
        if not self.quiet:
            print(msg)


    def _impute_data(self):
        """
        Impute data in-place updating self.snps by filling missing (9) values.
        """
        # simple imputer method
        # if self.impute_method == "simple":
        # self.snps = SNPsImputer(
        # self.snps, self.names, self.imap, None).run()

        if self.impute_method == "sample":
            self.snps = SNPsImputer(
                self.snps, self.names, self.imap, "sample", self.quiet).run()

        elif isinstance(self.impute_method, int):
            self.snps = self._impute_kmeans(
                self.topcov, self.niters, self.quiet)

        else:
            self.snps[self.snps == 9] = 0
            self._print(
                "Imputation (null; sets to 0): {:.1f}%, {:.1f}%, {:.1f}%"
                .format(100, 0, 0)            
            )


    def _impute_kmeans(self, topcov=0.9, niters=5, quiet=False):

        # the ML models to fit
        pca_model = decomposition.PCA(n_components=None)  # self.ncomponents)
        kmeans_model = KMeans(n_clusters=self.impute_method)

        # start kmeans with a global imap
        kmeans_imap = {'global': self.names}

        # iterate over step values
        iters = np.linspace(topcov, self.mincov, niters)
        for it, kmeans_mincov in enumerate(iters):

            # start message
            kmeans_minmap = {i: self.mincov for i in kmeans_imap}
            self._print(
                "Kmeans clustering: iter={}, K={}, mincov={}, minmap={}"
                .format(it, self.impute_method, kmeans_mincov, kmeans_minmap))

            # 1. Load orig data and filter with imap, minmap, mincov=step
            se = SNPsExtracter(
                self.data, 
                imap=kmeans_imap, 
                minmap=kmeans_minmap, 
                mincov=kmeans_mincov,
                quiet=self.quiet,
            )
            se.parse_genos_from_hdf5()

            # update snpsmap to new filtered data to use for subsampling            
            self.snpsmap = se.snpsmap

            # 2. Impute missing data using current kmeans clusters
            impdata = SNPsImputer(
                se.snps, se.names, kmeans_imap, "sample", self.quiet).run()

            # x. On final iteration return this imputed array as the result
            if it == 4:
                return impdata

            # 3. subsample unlinked SNPs
            subdata = impdata[:, jsubsample_snps(se.snpsmap, self._seed())]

            # 4. PCA on new imputed data values
            pcadata = pca_model.fit_transform(subdata)

            # 5. Kmeans clustering to find new imap grouping
            kmeans_model.fit(pcadata)
            labels = np.unique(kmeans_model.labels_)           
            kmeans_imap = {
                i: [se.names[j] for j in 
                    np.where(kmeans_model.labels_ == i)[0]] for i in labels
            }
            self._print(kmeans_imap)
            self._print("")


    def _run(self, seed, subsample, quiet):
        """
        Called inside .run(). A single iteration. 
        """
        # sample one SNP per locus
        if subsample:
            data = self.snps[:, jsubsample_snps(self.snpsmap, seed)]
            if not quiet:
                print(
                    "Subsampling SNPs: {}/{}"
                    .format(data.shape[1], self.snps.shape[1])
                )
        else:
            data = self.snps

        # decompose pca call
        model = decomposition.PCA(None)  # self.ncomponents)
        model.fit(data)
        newdata = model.transform(data)
        variance = model.explained_variance_ratio_

        # return tuple with new coordinates and variance explained
        return newdata, variance


    def run_and_plot_2D(self, ax0, ax1, seed=None, nreplicates=1, subsample=True, quiet=None):
        """
        Call .run() and .draw() in one single call. This is for simplicity. 
        In generaly you will probably want to call .run() and then .draw()
        as two separate calls. This way you can generate the results with .run()
        and then plot the stored results in many different ways using .draw().
        """
        # combine run and draw into one call for simplicity
        self.run(nreplicates=nreplicates, seed=seed, subsample=subsample, quiet=quiet)
        c, a, m = self.draw(ax0=ax0, ax1=ax1)
        return c, a, m


    def run(self, nreplicates=1, seed=None, subsample=True, quiet=None):
        """
        Decompose genotype array (.snps) into n_components axes. 

        Parameters:
        -----------
        nreplicates: (int)
            Number of replicate subsampled analyses to run. This is useful
            for exploring variation over replicate samples of unlinked SNPs.
            The .draw() function will show variation over replicates runs.
        seed: (int)
            Random number seed used if/when subsampling SNPs.
        subsample: (bool)
            Subsample one SNP per RAD locus to reduce effect of linkage.
        quiet: (bool)
            Print statements           

        Returns:
        --------      
        Two dctionaries are stored to the pca object in .pcaxes and .variances. 
        The first is the new data decomposed into principal coordinate space; 
        the second is an array with the variance explained by each PC axis. 
        """
        # default to 1 rep
        nreplicates = (nreplicates if nreplicates else 1)

        # option to override self.quiet for this run
        quiet = (quiet if quiet else self.quiet)

        # update seed. Numba seed cannot be None, so get random int if None
        seed = (seed if seed else self._seed())
        rng = np.random.RandomState(seed)

        # get data points for all replicate runs
        datas = {}
        vexps = {}
        datas[0], vexps[0] = self._run(
            subsample=subsample, 
            seed=rng.randint(0, 1e15), 
            quiet=quiet,
        )

        for idx in range(1, nreplicates):
            datas[idx], vexps[idx] = self._run(
                subsample=subsample, 
                seed=rng.randint(0, 1e15),
                quiet=True)

        # store results to object
        self.pcaxes = datas
        self.variances = vexps


    def draw(
        self, 
        ax0=0,
        ax1=1,
        cycle=8,
        colors=None,
        shapes=None,
        size=10,
        legend=True,
        width=400, 
        height=300,
        **kwargs):
        """
        Draw a scatterplot for data along two PC axes. 
        """
        # check for replicates in the data
        datas = self.pcaxes
        nreplicates = len(datas)
        variance = np.array([i for i in self.variances.values()]).mean(axis=0)

        # check that requested axes exist
        assert max(ax0, ax1) < self.pcaxes[0].shape[1], (
            "data set only has {} axes.".format(self.pcaxes[0].shape[1]))

        # test reversions of replicate axes (clumpp like) so that all plot
        # in the same orientation as replicate 0.
        model = LinearRegression()
        for i in range(1, len(datas)):
            for ax in [ax0, ax1]:
                orig = datas[0][:, ax].reshape(-1, 1)
                new = datas[i][:, ax].reshape(-1, 1)
                swap = (datas[i][:, ax] * -1).reshape(-1, 1)

                # get r^2 for both model fits
                model.fit(orig, new)
                c0 = model.coef_[0][0]
                model.fit(orig, swap)
                c1 = model.coef_[0][0]

                # if swapped fit is better make this the data
                if c1 > c0:
                    datas[i][:, ax] = datas[i][:, ax] * -1

        # make reverse imap dictionary
        irev = {}
        for pop, vals in self.imap.items():
            for val in vals:
                irev[val] = pop

        # the max number of pops until color cycle repeats
        cycle = min(cycle, len(self.imap))

        # get color list repeating in cycles of cycle
        if not colors:
            colors = itertools.cycle(
                toyplot.color.broadcast(
                    toyplot.color.brewer.map("Spectral"), shape=cycle,
                )
            )
        else:
            colors = iter(colors)
            # assert len(colors) == len(imap), "len colors must match len imap"

        # get shapes list repeating in cycles of cycle up to 5 * cycle
        if not shapes:
            shapes = itertools.cycle(np.concatenate([
                np.tile("o", cycle),
                np.tile("s", cycle),
                np.tile("^", cycle),
                np.tile("d", cycle),
                np.tile("v", cycle),
                np.tile("<", cycle),
                np.tile("x", cycle),            
            ]))
        else:
            shapes = iter(shapes)
        # else:
            # assert len(shapes) == len(imap), "len colors must match len imap"            

        # assign styles to populations and to legend markers (no replicates)
        pstyles = {}
        rstyles = {}
        for idx, pop in enumerate(self.imap):

            color = next(colors)
            shape = next(shapes)

            pstyles[pop] = toyplot.marker.create(
                size=size, 
                shape=shape,
                mstyle={
                    "fill": toyplot.color.to_css(color),
                    "stroke": "#262626",
                    "stroke-width": 1.0,
                    "fill-opacity": 0.75,
                },
            )
            rstyles[pop] = toyplot.marker.create(
                size=size, 
                shape=shape,
                mstyle={
                    "fill": toyplot.color.to_css(color),
                    "stroke": "none",
                    "fill-opacity": 0.9 / nreplicates,
                },
            )            

        # assign styled markers to data points
        pmarks = []
        rmarks = []
        for name in self.names:
            pop = irev[name]
            pmark = pstyles[pop]
            pmarks.append(pmark)
            rmark = rstyles[pop]
            rmarks.append(rmark)

        # get axis labels for PCA or TSNE plot
        if variance[ax0] >= 0.0:
            xlab = "PC{} ({:.1f}%) explained".format(ax0, variance[ax0] * 100)
            ylab = "PC{} ({:.1f}%) explained".format(ax1, variance[ax1] * 100)
        else:
            xlab = "TNSE component 1"
            ylab = "TNSE component 2"            

        # plot points with colors x population
        canvas = toyplot.Canvas(width, height)  # 400, 300)
        axes = canvas.cartesian(
            grid=(1, 5, 0, 1, 0, 4),
            xlabel=xlab,
            ylabel=ylab,
        )

        # if not replicates then just plot the points
        if nreplicates < 2:
            mark = axes.scatterplot(
                datas[0][:, ax0],
                datas[0][:, ax1],
                marker=pmarks,
                title=self.names,
            )

        # replicates show clouds plus centroids
        else:
            # add the replicates cloud points       
            for i in range(nreplicates):
                # get transformed coordinates and variances
                mark = axes.scatterplot(
                    datas[i][:, ax0],
                    datas[i][:, ax1],
                    marker=rmarks,
                )

            # compute centroids
            Xarr = np.concatenate(
                [
                    np.array([datas[i][:, ax0], datas[i][:, ax1]]).T 
                    for i in range(nreplicates)
                ]
            )
            yarr = np.tile(np.arange(len(self.names)), nreplicates)
            clf = NearestCentroid()
            clf.fit(Xarr, yarr)

            # draw centroids
            mark = axes.scatterplot(
                clf.centroids_[:, 0],
                clf.centroids_[:, 1],
                title=self.names,
                marker=pmarks,
            )

        # add a legend
        if legend:
            if len(self.imap) > 1:
                marks = [(pop, marker) for pop, marker in pstyles.items()]
                canvas.legend(
                    marks, 
                    corner=("right", 35, 100, min(250, len(pstyles) * 25))
                )
        return canvas, axes, mark


    def run_tsne(self, subsample=True, perplexity=5.0, n_iter=1e6, seed=None):
        """
        Calls TSNE model from scikit-learn on 
        """
        seed = (seed if seed else self._seed())
        if subsample:
            data = self.snps[:, jsubsample_snps(self.snpsmap, seed)]
            print(
                "Subsampling SNPs: {}/{}"
                .format(data.shape[1], self.snps.shape[1])
            )
        else:
            data = self.snps

        # init TSNE model object with params (sensitive)
        tsne_model = TSNE(
            perplexity=perplexity,
            init='pca', 
            n_iter=int(n_iter), 
            random_state=seed,
        )

        # fit the model
        tsne_data = tsne_model.fit_transform(data)
        self.pcaxes = {0: tsne_data}
        self.variances = {0: [-1.0, -2.0]}



    # def run_and_plot_2D(
    #     self, 
    #     ax0=0, 
    #     ax1=1, 
    #     seed=None, 
    #     subsample=True, 
    #     nreplicates=None,
    #     # model="pca",
    #     ):
    #     """
    #     A convenience function for plotting 2D scatterplot of PCA results.
    #     """

    #     # plot canvas
    #     canvas, axes, mark = self.plot_2D(ax0, ax1, datas, vexps)
    #     return canvas, axes, mark
