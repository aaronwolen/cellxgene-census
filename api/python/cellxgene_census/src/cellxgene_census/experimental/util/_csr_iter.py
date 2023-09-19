from typing import Generator, Iterator, Tuple

import numpy as np
import numpy.typing as npt
import pandas as pd
import scipy.sparse as sparse
import tiledbsoma as soma
from typing_extensions import Literal

from ._eager_iter import _EagerIterator

_RT = Tuple[Tuple[npt.NDArray[np.int64], npt.NDArray[np.int64]], sparse.spmatrix]


def X_sparse_iter(
    query: soma.ExperimentAxisQuery,
    X_name: str = "raw",
    axis: int = 0,
    stride: int = 2**16,
    fmt: Literal["csr", "csc"] = "csr",
    use_eager_fetch: bool = True,
    reindex_sparse_axis: bool = True,
) -> Iterator[_RT]:
    """
    Iterate over rows (or columns) of the query results X matrix, with pagination to
    control peak memory usage for large result sets. Each iteration step yields:
        * obs_coords (coordinates)
        * var_coords (coordinates)
        * a chunk of X contents as a SciPy csr_matrix or csc_matrix

    The coordinates and X matrix chunks are indexed positionally, i.e. for any
    given value in the matrix, X[i, j], the original soma_joinid (aka soma_dim_0
    and soma_dim_1) are present in obs_coords[i] and var_coords[j].

    Args:
        query:
            A SOMA ExperimentAxisQuery defining the coordinates over which the iterator will
            read.
        X_name:
            The name of the X layer.
        axis:
            The axis to iterate over, where zero (0) is obs axis and one (1)
            is the var axis. Currently only axis 0 (obs axis) is supported.
        stride:
            The chunk size to return in each step (number of obs rows or var columns).
        fmt:
            The SciPy sparse array layout. Supported: 'csc' and 'csr' (default).
        use_eager_fetch:
            If true, will use multiple threads to parallelize reading
            and processing. This will improve speed, but at the cost
            of some additional memory use.
        reindex_sparse_axis:
            If false, then the sparse axis of each X chunk (var for csr, obs for csc)
            will be indexed by soma_joinid, instead of reindexed by var_coords or
            obs_coords position, respectively. Skipping the reindexing streamlines the
            operation slightly if the application prefers to address the axis by
            soma_joinid directly, or if it needs to reindex the axis in some other way
            regardless.
            The other axis (obs for csr, var for csc) is always reindexed in order to
            control memory usage, as scipy.sparse stores the underlying indptr array
            densely.

    Returns:
        An iterator which iterates over a tuple of:
            (obs_coords, var_coords)
            SciPy sparse matrix

    Examples:
        >>> with cellxgene_census.open_soma() as census:
        ...     exp = census["census_data"][experiment]
        ...     with exp.axis_query(measurement_name="RNA") as query:
        ...         for (obs_soma_joinids, var_soma_joinids), X_chunk in X_sparse_iter(
        ...             query, X_name="raw", stride=1000
        ...         ):
        ...             # X_chunk is a scipy.csr_matrix of csc_matrix
        ...             # For each X_chunk[i, j], the associated soma_joinid is
        ...             # obs_soma_joinids[i] and var_soma_joinids[j]
        ...             ...

    Lifecycle:
        experimental

    See also: https://github.com/single-cell-data/TileDB-SOMA/issues/1528
    """
    reindex_obs = True
    reindex_var = True
    if fmt == "csr":
        fmt_ctor = sparse.csr_matrix
        reindex_var = reindex_sparse_axis
    elif fmt == "csc":
        fmt_ctor = sparse.csc_matrix
        reindex_obs = reindex_sparse_axis
    else:
        raise ValueError("fmt must be 'csr' or 'csc'")
    assert reindex_obs or reindex_var

    if axis != 0:
        raise ValueError("axis must be zero (obs)")

    # Lazy partition array by chunk_size on first dimension
    obs_coords = query.obs_joinids().to_numpy()
    obs_coord_chunker = (obs_coords[i : i + stride] for i in range(0, len(obs_coords), stride))

    X = query._ms.X[X_name]
    var_coords = query.var_joinids().to_numpy()

    # Lazy read into Arrow Table. Yields (coords, Arrow.Table)
    table_reader = (
        (
            (obs_coords_chunk, var_coords),
            X.read(coords=(obs_coords_chunk, var_coords)).tables().concat(),
        )
        for obs_coords_chunk in obs_coord_chunker
    )
    if use_eager_fetch:
        table_reader = (t for t in _EagerIterator(table_reader, query._threadpool))

    # Lazy reindexing of soma_dim_0 and soma_dim_1 to obs_coords and var_coords
    # positions (except one or the other if not reindex_sparse_axis).
    # Yields (obs_coords, var_coords) and COO (data, i, j) as numpy ndarrays
    coo_reindexer = (
        (
            (obs_coords_chunk, var_coords),
            (
                tbl["soma_data"].to_numpy(),
                (
                    pd.Index(obs_coords_chunk).get_indexer(tbl["soma_dim_0"].to_numpy())
                    if reindex_obs
                    else tbl["soma_dim_0"].to_numpy()
                ),
                (query.indexer.by_var(tbl["soma_dim_1"].to_numpy()) if reindex_var else tbl["soma_dim_1"].to_numpy()),
            ),
        )
        for (obs_coords_chunk, var_coords), tbl in table_reader
    )
    if use_eager_fetch:
        coo_reindexer = (t for t in _EagerIterator(coo_reindexer, query._threadpool))

    # Lazy convert COO to Scipy sparse matrix (csr or csc according to fmt)
    fmt_reader: Generator[_RT, None, None] = (
        (
            (obs_coords_chunk, var_coords),
            fmt_ctor(
                sparse.coo_matrix(
                    (data, (i, j)),
                    shape=(
                        (len(obs_coords_chunk) if reindex_obs else X.shape[0]),
                        (query.n_vars if reindex_var else X.shape[1]),
                    ),
                )
            ),
        )
        for (obs_coords_chunk, var_coords), (data, i, j) in coo_reindexer
    )
    if use_eager_fetch:
        fmt_reader = (t for t in _EagerIterator(fmt_reader, query._threadpool))

    yield from fmt_reader
