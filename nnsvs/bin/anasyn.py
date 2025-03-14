import os
from os.path import join
from pathlib import Path

import hydra
import numpy as np
import pyworld
import torch
from hydra.utils import to_absolute_path
from nnsvs.dsp import bandpass_filter
from nnsvs.gen import gen_world_params
from nnsvs.logger import getLogger
from nnsvs.multistream import get_static_stream_sizes, split_streams
from nnsvs.usfgan import USFGANWrapper
from nnsvs.util import StandardScaler, init_seed, load_utt_list
from omegaconf import DictConfig, OmegaConf
from parallel_wavegan.utils import load_model
from scipy.io import wavfile
from tqdm.auto import tqdm


@torch.no_grad()
def anasyn(
    device,
    acoustic_features,
    acoustic_config,
    vocoder=None,
    vocoder_config=None,
    vocoder_in_scaler=None,
    sample_rate=48000,
    frame_period=5,
    vuv_threshold=0.5,
    use_world_codec=True,
    feature_type="world",
    vocoder_type="world",
):
    static_stream_sizes = get_static_stream_sizes(
        acoustic_config.stream_sizes,
        acoustic_config.has_dynamic_features,
        acoustic_config.num_windows,
    )

    # Split multi-stream features
    streams = split_streams(acoustic_features, static_stream_sizes)

    # Generate WORLD parameters
    if feature_type == "world":
        assert len(streams) == 4
        mgc, lf0, vuv, bap = streams
    elif feature_type == "melf0":
        mel, lf0, vuv = split_streams(acoustic_features, [80, 1, 1])
    else:
        raise ValueError(f"Unknown feature type: {feature_type}")

    # Waveform generation by (1) WORLD or (2) neural vocoder
    if vocoder_type == "world":
        f0, spectrogram, aperiodicity = gen_world_params(
            mgc,
            lf0,
            vuv,
            bap,
            sample_rate,
            vuv_threshold=vuv_threshold,
            use_world_codec=use_world_codec,
        )
        wav = pyworld.synthesize(
            f0,
            spectrogram,
            aperiodicity,
            sample_rate,
            frame_period,
        )
    elif vocoder_type == "pwg":
        # NOTE: So far vocoder models are trained on binary V/UV features
        vuv = (vuv > vuv_threshold).astype(np.float32)
        if feature_type == "world":
            voc_inp = (
                torch.from_numpy(
                    vocoder_in_scaler.transform(
                        np.concatenate([mgc, lf0, vuv, bap], axis=-1)
                    )
                )
                .float()
                .to(device)
            )
        elif feature_type == "melf0":
            voc_inp = (
                torch.from_numpy(
                    vocoder_in_scaler.transform(
                        np.concatenate([mel, lf0, vuv], axis=-1)
                    )
                )
                .float()
                .to(device)
            )
        wav = vocoder.inference(voc_inp).view(-1).to("cpu").numpy()
    elif vocoder_type == "usfgan":
        if feature_type == "world":
            fftlen = pyworld.get_cheaptrick_fft_size(sample_rate)
            aperiodicity = pyworld.decode_aperiodicity(
                np.ascontiguousarray(bap).astype(np.float64), sample_rate, fftlen
            )
            # fill aperiodicity with ones for unvoiced regions
            aperiodicity[vuv.reshape(-1) < vuv_threshold, 0] = 1.0
            # WORLD fails catastrophically for out of range aperiodicity
            aperiodicity = np.clip(aperiodicity, 0.0, 1.0)
            # back to bap
            bap = pyworld.code_aperiodicity(aperiodicity, sample_rate).astype(
                np.float32
            )

            aux_feats = (
                torch.from_numpy(
                    vocoder_in_scaler.transform(np.concatenate([mgc, bap], axis=-1))
                )
                .float()
                .to(device)
            )
        elif feature_type == "melf0":
            # NOTE: So far vocoder models are trained on binary V/UV features
            vuv = (vuv > vuv_threshold).astype(np.float32)
            aux_feats = (
                torch.from_numpy(vocoder_in_scaler.transform(mel)).float().to(device)
            )
        contf0 = np.exp(lf0)
        if vocoder_config.data.sine_f0_type == "contf0":
            f0_inp = contf0
        elif vocoder_config.data.sine_f0_type == "f0":
            f0_inp = contf0
            f0_inp[vuv < vuv_threshold] = 0
        wav = vocoder.inference(f0_inp, aux_feats).view(-1).to("cpu").numpy()

    return wav


def post_process(wav, sample_rate):
    wav = bandpass_filter(wav, sample_rate)

    if np.max(wav) > 10:
        if np.abs(wav).max() > 32767:
            wav = wav / np.abs(wav).max()
        # data is likely already in [-32768, 32767]
        wav = wav.astype(np.int16)
    else:
        if np.abs(wav).max() > 1.0:
            wav = wav / np.abs(wav).max()
        wav = (wav * 32767.0).astype(np.int16)
    return wav


@hydra.main(config_path="conf/synthesis", config_name="config")
def my_app(config: DictConfig) -> None:
    global logger
    logger = getLogger(config.verbose)
    logger.info(OmegaConf.to_yaml(config))

    if not torch.cuda.is_available():
        device = torch.device("cpu")
    else:
        device = torch.device(config.device)

    acoustic_config = OmegaConf.load(to_absolute_path(config.acoustic.model_yaml))

    # Vocoder
    if config.vocoder.checkpoint is not None and len(config.vocoder.checkpoint) > 0:
        path = Path(to_absolute_path(config.vocoder.checkpoint))
        vocoder_dir = path.parent
        if (vocoder_dir / "vocoder_model.yaml").exists():
            # packed model
            vocoder_config = OmegaConf.load(vocoder_dir / "vocoder_model.yaml")
        elif (vocoder_dir / "config.yml").exists():
            # PWG checkpoint
            vocoder_config = OmegaConf.load(vocoder_dir / "config.yml")
        else:
            # usfgan
            vocoder_config = OmegaConf.load(vocoder_dir / "config.yaml")

        if "generator" in vocoder_config and "discriminator" in vocoder_config:
            # usfgan
            checkpoint = torch.load(
                path,
                map_location=lambda storage, loc: storage,
            )
            vocoder = hydra.utils.instantiate(vocoder_config.generator).to(device)
            vocoder.load_state_dict(checkpoint["model"]["generator"])
            vocoder.remove_weight_norm()
            vocoder = USFGANWrapper(vocoder_config, vocoder)

            # Extract scaler params for [mgc, bap]
            if vocoder_config.data.aux_feats == ["mcep", "codeap"]:
                mean_ = np.load(vocoder_dir / "in_vocoder_scaler_mean.npy")
                var_ = np.load(vocoder_dir / "in_vocoder_scaler_var.npy")
                scale_ = np.load(vocoder_dir / "in_vocoder_scaler_scale.npy")
                stream_sizes = get_static_stream_sizes(
                    acoustic_config.stream_sizes,
                    acoustic_config.has_dynamic_features,
                    acoustic_config.num_windows,
                )
                mgc_end_dim = stream_sizes[0]
                bap_start_dim = sum(stream_sizes[:3])
                bap_end_dim = sum(stream_sizes[:4])
                vocoder_in_scaler = StandardScaler(
                    np.concatenate(
                        [mean_[:mgc_end_dim], mean_[bap_start_dim:bap_end_dim]]
                    ),
                    np.concatenate(
                        [var_[:mgc_end_dim], var_[bap_start_dim:bap_end_dim]]
                    ),
                    np.concatenate(
                        [scale_[:mgc_end_dim], scale_[bap_start_dim:bap_end_dim]]
                    ),
                )
            else:
                vocoder_in_scaler = StandardScaler(
                    np.load(vocoder_dir / "in_vocoder_scaler_mean.npy")[:80],
                    np.load(vocoder_dir / "in_vocoder_scaler_var.npy")[:80],
                    np.load(vocoder_dir / "in_vocoder_scaler_scale.npy")[:80],
                )
        else:
            # Normal pwg
            vocoder = load_model(path, config=vocoder_config).to(device)
            vocoder.remove_weight_norm()
            vocoder_in_scaler = StandardScaler(
                np.load(vocoder_dir / "in_vocoder_scaler_mean.npy"),
                np.load(vocoder_dir / "in_vocoder_scaler_var.npy"),
                np.load(vocoder_dir / "in_vocoder_scaler_scale.npy"),
            )

        vocoder.eval()
    else:
        vocoder = None
        vocoder_config = None
        vocoder_in_scaler = None
        if config.synthesis.vocoder_type != "world":
            logger.warning("Vocoder checkpoint is not specified")
            logger.info(f"Use world instead of {config.synthesis.vocoder_type}.")
        config.synthesis.vocoder_type = "world"

    # Run synthesis for each utt.

    in_dir = to_absolute_path(config.in_dir)
    out_dir = to_absolute_path(config.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    utt_ids = load_utt_list(to_absolute_path(config.utt_list))
    logger.info("Processes %s utterances...", len(utt_ids))
    for utt_id in tqdm(utt_ids):
        acoustic_features = np.load(join(in_dir, f"{utt_id}-feats.npy"))
        init_seed(1234)

        wav = anasyn(
            device=device,
            acoustic_features=acoustic_features,
            acoustic_config=acoustic_config,
            vocoder=vocoder,
            vocoder_config=vocoder_config,
            vocoder_in_scaler=vocoder_in_scaler,
            sample_rate=config.synthesis.sample_rate,
            frame_period=config.synthesis.frame_period,
            use_world_codec=config.synthesis.use_world_codec,
            feature_type=config.synthesis.feature_type,
            vocoder_type=config.synthesis.vocoder_type,
            vuv_threshold=config.synthesis.vuv_threshold,
        )
        wav = post_process(wav, config.synthesis.sample_rate)
        out_wav_path = join(out_dir, f"{utt_id}.wav")
        wavfile.write(
            out_wav_path, rate=config.synthesis.sample_rate, data=wav.astype(np.int16)
        )


def entry():
    my_app()  # pylint: disable=no-value-for-parameter


if __name__ == "__main__":
    my_app()  # pylint: disable=no-value-for-parameter
