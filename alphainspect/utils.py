from typing import Sequence, Optional

import numpy as np
import pandas as pd
import polars as pl
from polars import selectors as cs
from polars_ta.wq import cs_qcut, cs_top_bottom

from alphainspect import _QUANTILE_, _DATE_
from alphainspect._nb import _sub_portfolio_returns


def with_factor_quantile(df: pl.DataFrame, factor: str, quantiles: int = 10, group_name: Optional[str] = None, factor_quantile: str = _QUANTILE_) -> pl.DataFrame:
    """添加因子分位数信息

    Parameters
    ----------
    df
    factor
        因子名
    quantiles
        分层数
    group_name
        条件分组
    factor_quantile
        分组名

    """
    by = [_DATE_]
    if group_name is not None:
        if isinstance(group_name, str):
            group_name = [group_name]
        by.extend(group_name)

    df = df.with_columns(cs_qcut(pl.col(factor).fill_nan(None), quantiles).over(*by).alias(factor_quantile))
    return df


def with_factor_top_k(df: pl.DataFrame, factor: str, top_k: int = 10, group_name: Optional[str] = None, factor_quantile: str = _QUANTILE_) -> pl.DataFrame:
    """前K后K的分层方法。一般用于截面股票数不多无法分位数分层的情况。

    Parameters
    ----------
    df
    factor
    top_k
    group_name
    factor_quantile

    Returns
    -------
    pl.DataFrame
        输出范围为0、1、2，分别为做空、对冲、做多。输出数量并不等于top_k，

        - 遇到重复时会出现数量大于top_k，
        - 遇到top_k>数量/2时，即在做多组又在做空组会划分到对冲组

    """
    by = [_DATE_]
    if group_name is not None:
        if isinstance(group_name, str):
            group_name = [group_name]
        by.extend(group_name)

    df = df.with_columns(cs_top_bottom(pl.col(factor).fill_nan(None), top_k).over(*by).alias(factor_quantile))
    df = df.with_columns(pl.col(factor_quantile) + 1)
    return df


def with_industry(df: pl.DataFrame, industry_name: str):
    """添加行业列

    Parameters
    ----------
    df
    industry_name
        行业名称。哑元变量扩充

    Notes
    -----
    `to_dummies(drop_first=True)`丢弃哪个字段是随机的，非常不友好，只能在行业中性化时动态修改代码

    """
    df = df.with_columns([
        # 行业处理，由浮点改成整数
        pl.col(industry_name).fill_nan(None).fill_null(0).cast(pl.UInt32),
    ])

    df = df.with_columns(df.to_dummies(industry_name, drop_first=True))
    return df


def cumulative_returns(returns: np.ndarray, weights: np.ndarray,
                       funds: int = 3, freq: int = 3,
                       benchmark: np.ndarray = None,
                       ret_mean: bool = True,
                       init_cash: float = 1.0,
                       risk_free: float = 1.0,  # 1.0 + 0.025 / 250
                       ) -> np.ndarray:
    """累积收益

    精确计算收益是非常麻烦的事情，比如考虑手续费、滑点、涨跌停无法入场。考虑过多也会导致计算量巨大。
    这里只做估算，用于不同因子之间收益比较基本够用。更精确的计算请使用专用的回测引擎

    需求：因子每天更新，但策略是持仓3天
    1. 每3天取一次因子，并持有3天。即入场时间对净值影响很大。净值波动剧烈
    2. 资金分成3份，每天入场一份。每份隔3天做一次调仓，多份资金不共享。净值波动平滑

    本函数使用的第2种方法，例如：某支股票持仓信息如下
    [0,1,1,1,0,0]
    资金分成三份，每次持有三天，
    [0,0,0,1,1,1] # 第0、3、6...位，fill后两格
    [0,1,1,1,0,0] # 第1、4、7...位，fill后两格
    [0,0,1,1,1,0] # 第2、5、8...位，fill后两格

    Parameters
    ----------
    returns: np.ndarray
        1期简单收益率。自动记在出场位置。
    weights: np.ndarray
        持仓权重。需要将信号移动到出场日期。权重绝对值和
    funds: int
        资金拆成多少份
    freq:int
        再调仓频率
    benchmark: 1d np.ndarray
        基准收益率
    ret_mean: bool
        返回多份资金合成曲线
    init_cash: float
        初始资金
    risk_free: float
        无风险收益率。用在现金列。空仓时，可以给现金提供利息

    Returns
    -------
    np.ndarray

    References
    ----------
    https://github.com/quantopian/alphalens/issues/187

    """
    # 一维修改成二维，代码统一
    if returns.ndim == 1:
        returns = returns.reshape(-1, 1)
    if weights.ndim == 1:
        weights = weights.reshape(-1, 1)

    # 形状
    m, n = weights.shape

    # 现金权重
    weights_cash = 1 - np.round(np.nansum(np.abs(weights), axis=1), 5)
    # TODO 也可以添加两列现金，一列有利息，一列没利息。细节要按策略进行定制
    returns = np.concatenate((np.ones(shape=(m, 1), dtype=returns.dtype), returns), axis=1)
    weights = np.concatenate((np.zeros(shape=(m, 1), dtype=weights.dtype), weights), axis=1)
    # 添加第0列做为现金，用于处理CTA空仓的问题
    weights[:, 0] = weights_cash
    # 可以考虑给现金指定一个固定收益
    returns[:, 0] = risk_free

    # 修正数据中出现的nan
    returns = np.where(returns == returns, returns, 1.0)
    # 权重需要已经分配好，绝对值和为1
    weights = np.where(weights == weights, weights, 0.0)

    # 新形状
    m, n = weights.shape

    #  记录每份资金每期收益率
    out = _sub_portfolio_returns(m, n, weights, returns, funds, freq, init_cash)
    if ret_mean:
        if benchmark is None:
            # 多份净值直接叠加后平均
            return out.mean(axis=1)
        else:
            # 有基准，计算超额收益
            return out.mean(axis=1) - (benchmark + 1).cumprod()
    else:
        return out


def select_by_suffix(df: pl.DataFrame, name: str) -> pl.DataFrame:
    """选择指定后缀的所有因子"""
    return df.select(cs.ends_with(name).name.map(lambda x: x[:-len(name)]))


def select_by_prefix(df: pl.DataFrame, name: str) -> pl.DataFrame:
    """选择指定前缀的所有因子"""
    return df.select(cs.starts_with(name).name.map(lambda x: x[len(name):]))


# =================================
# 没分好类的函数先放这，等以后再移动
def symmetric_orthogonal(matrix):
    # 计算特征值和特征向量
    eigenvalues, eigenvectors = np.linalg.eigh(matrix)

    # 按照特征值的大小排序
    sorted_indices = np.argsort(eigenvalues)[::-1]
    sorted_eigenvectors = eigenvectors[:, sorted_indices]

    # 正交化矩阵
    orthogonal_matrix = np.linalg.qr(sorted_eigenvectors)[0]

    return orthogonal_matrix


def row_unstack(df: pl.DataFrame, index: Sequence[str], columns: Sequence[str]) -> pd.DataFrame:
    """一行值堆叠成一个矩阵"""
    return pd.DataFrame(df.to_numpy().reshape(len(index), len(columns)),
                        index=index, columns=columns)


def index_split_unstack(s: pd.Series, split_by: str = '__'):
    s.index = pd.MultiIndex.from_tuples(map(lambda x: tuple(x.split(split_by)), s.index))
    return s.unstack()
