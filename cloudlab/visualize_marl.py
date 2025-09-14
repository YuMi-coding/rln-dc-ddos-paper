#!/usr/bin/env python3
# visualize_marl.py
import argparse, os, glob, json, math
from typing import Dict, List, Tuple
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

def _safe_mkdir(p: str):
    os.makedirs(p, exist_ok=True)

def load_metrics_summary(log_dir: str) -> pd.DataFrame:
    path = os.path.join(log_dir, "metrics_summary.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing {path}. Make sure marl_py3.py ran with log writing enabled.")
    df = pd.read_csv(path)
    df = df.sort_values("episode").reset_index(drop=True)
    return df

def load_actions(log_dir: str) -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(log_dir, "actions_ep*.csv")))
    if not files:
        return pd.DataFrame()
    frames = []
    for f in files:
        # actions CSV columns: step,learner,src_ip,dst_ip,allow_prob,pred_bad,truth_bad
        ep = int(os.path.basename(f)[len("actions_ep"):len("actions_ep")+4])
        d = pd.read_csv(f)
        d["episode"] = ep
        frames.append(d)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

def recompute_metrics_from_actions(actions: pd.DataFrame) -> pd.DataFrame:
    """
    Recompute per-episode confusion and accuracy using the last prediction per src_ip
    (to match your end-of-episode scoring logic).
    """
    rows = []
    for ep, grp in actions.groupby("episode"):
        # keep only the last record per src_ip
        last = grp.sort_values("step").groupby("src_ip").tail(1)
        truth_bad = last["truth_bad"].astype(int).values
        pred_bad  = last["pred_bad"].astype(int).values
        tp = int(((truth_bad == 1) & (pred_bad == 1)).sum())
        tn = int(((truth_bad == 0) & (pred_bad == 0)).sum())
        fp = int(((truth_bad == 0) & (pred_bad == 1)).sum())
        fn = int(((truth_bad == 1) & (pred_bad == 0)).sum())
        support = tp + tn + fp + fn
        acc  = (tp + tn)/support if support else 0.0
        prec = tp/(tp+fp) if (tp+fp) else 0.0
        rec  = tp/(tp+fn) if (tp+fn) else 0.0
        f1   = (2*prec*rec)/(prec+rec) if (prec+rec) else 0.0
        rows.append({"episode": ep, "tp": tp, "tn": tn, "fp": fp, "fn": fn,
                     "support": support, "accuracy_recalc": acc,
                     "precision_recalc": prec, "recall_recalc": rec, "f1_recalc": f1})
    return pd.DataFrame(rows).sort_values("episode").reset_index(drop=True)

def quick_roc_from_actions(actions: pd.DataFrame) -> pd.DataFrame:
    """
    Build a single 'overall' ROC (across episodes) using allow_prob as score for "good".
    We convert to 'bad' score = 1 - allow_prob.
    """
    if actions.empty:
        return pd.DataFrame()
    df = actions.copy()
    # Use the last prediction per (episode, src_ip) to avoid bias
    df = df.sort_values(["episode","step"]).groupby(["episode","src_ip"]).tail(1)
    y = df["truth_bad"].astype(int).values
    scores = 1.0 - df["allow_prob"].astype(float).values  # higher means "more likely bad"
    # thresholds over unique scores
    uniq = np.unique(scores)
    # add endpoints
    thresholds = np.r_[[-np.inf], uniq, [np.inf]]
    tpr, fpr = [], []
    P = (y==1).sum(); N = (y==0).sum()
    for t in thresholds:
        pred = (scores >= t).astype(int)
        tp = ((pred==1)&(y==1)).sum()
        fp = ((pred==1)&(y==0)).sum()
        fn = ((pred==0)&(y==1)).sum()
        tn = ((pred==0)&(y==0)).sum()
        tpr.append(tp/max(1,P))
        fpr.append(fp/max(1,N))
    return pd.DataFrame({"fpr": fpr, "tpr": tpr})

def plot_series(df: pd.DataFrame, cols: List[str], title: str, out: str = None, show: bool = False):
    plt.figure()
    x = df["episode"].values
    for c in cols:
        if c in df.columns:
            plt.plot(x, df[c].values, label=c)
    plt.xlabel("Episode")
    plt.ylabel("Value")
    plt.title(title)
    plt.legend()
    plt.grid(True, alpha=0.3)
    if out:
        plt.savefig(out, bbox_inches="tight", dpi=150)
    if show:
        plt.show()
    plt.close()

def main():
    ap = argparse.ArgumentParser(description="Visualize MARL training logs")
    ap.add_argument("--log-dir", default="logs", help="Directory with metrics_summary.csv and actions_ep*.csv")
    ap.add_argument("--show", action="store_true", help="Show interactive plots")
    ap.add_argument("--save-png", action="store_true", help="Save PNGs in the log dir")
    args = ap.parse_args()

    _safe_mkdir(args.log_dir)

    # 1) Metrics summary (from marl_py3.py)
    summary = load_metrics_summary(args.log_dir)
    print("==> Loaded metrics_summary.csv with", len(summary), "episodes")
    # Print a quick text summary
    last = summary.iloc[-1]
    print(f"Final episode {int(last['episode'])}: "
          f"acc={last['accuracy']:.3f}, prec={last['precision']:.3f}, "
          f"recall={last['recall']:.3f}, f1={last['f1']:.3f}, coverage={last['coverage']:.3f}")

    png1 = os.path.join(args.log_dir, "episodes_accuracy.png") if args.save_png else None
    plot_series(summary, ["accuracy","precision","recall","f1","coverage"],
                "Episode Metrics", png1, args.show)

    # 2) Optional: recompute from actions and compare
    actions = load_actions(args.log_dir)
    if not actions.empty:
        print(f"==> Loaded {len(actions)} action rows from actions_ep*.csv")
        recomputed = recompute_metrics_from_actions(actions)
        merged = pd.merge(summary, recomputed, on="episode", how="left")
        png2 = os.path.join(args.log_dir, "episodes_accuracy_recalc.png") if args.save_png else None
        plot_series(merged, ["accuracy","accuracy_recalc","f1","f1_recalc"],
                    "Episode Metrics (logged vs. recomputed)", png2, args.show)

        # 3) Optional global ROC from actions
        roc = quick_roc_from_actions(actions)
        if not roc.empty:
            plt.figure()
            plt.plot(roc["fpr"], roc["tpr"])
            plt.plot([0,1],[0,1], linestyle="--", linewidth=1)
            plt.xlabel("False Positive Rate")
            plt.ylabel("True Positive Rate")
            plt.title("Overall ROC (from actions)")
            plt.grid(True, alpha=0.3)
            if args.save_png:
                plt.savefig(os.path.join(args.log_dir, "overall_roc.png"), bbox_inches="tight", dpi=150)
            if args.show:
                plt.show()
            plt.close()
    else:
        print("Note: No actions_ep*.csv found — skipping recomputed metrics and ROC.")

    # 4) Reward per episode (if you later log it)
    # If you decide to dump per-episode reward to logs/reward_summary.csv, this will plot it.
    reward_csv = os.path.join(args.log_dir, "reward_summary.csv")
    if os.path.exists(reward_csv):
        r = pd.read_csv(reward_csv).sort_values("episode")
        png3 = os.path.join(args.log_dir, "episodes_reward.png") if args.save_png else None
        plot_series(r, ["avg_reward","median_reward"], "Episode Reward", png3, args.show)
    else:
        print("Tip: To plot reward, write per-episode stats to logs/reward_summary.csv "
              "with columns: episode,avg_reward,median_reward.")

if __name__ == "__main__":
    main()
