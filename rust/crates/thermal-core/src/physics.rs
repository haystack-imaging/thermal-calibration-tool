//! Grey-body emissivity relations (broadband Stefan–Boltzmann approximation).

use crate::Result;

const T0_K: f64 = 273.15;

/// Forward model: the apparent temperature a sensor set to ε = 1 reports for a surface at `t_true`
/// (°C) with emissivity `emissivity`, reflecting a sky at `t_sky` (°C).
pub fn apparent_temperature(t_true: f64, emissivity: f64, t_sky: f64) -> f64 {
    let tk = t_true + T0_K;
    let sk = t_sky + T0_K;
    let app4 = emissivity * tk.powi(4) + (1.0 - emissivity) * sk.powi(4);
    app4.max(0.0).powf(0.25) - T0_K
}

/// Apparent → surface temperature for a grey body, in place:
/// `T_obj⁴ = (T_app⁴ − (1 − ε)·T_sky⁴) / ε`. Computed in f64 per pixel and stored as f32, like the
/// Python tool. Note that the DJI SDK already applies its own emissivity (0.95 by default) when
/// converting R-JPEGs, so this is only right for images converted with emissivity 1.0.
pub fn apply_emissivity_correction(t_app: &mut [f32], emissivity: f64, t_sky: f64) -> Result<()> {
    if !(emissivity > 0.0 && emissivity <= 1.0) {
        return Err("emissivity must be in (0, 1]".into());
    }
    let sk4 = (t_sky + T0_K).powi(4);
    for p in t_app.iter_mut() {
        let tk = *p as f64 + T0_K;
        let obj4 = (tk.powi(4) - (1.0 - emissivity) * sk4) / emissivity;
        *p = (obj4.max(0.0).powf(0.25) - T0_K) as f32;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn forward_and_inverse_agree() {
        let t = 31.7;
        let app = apparent_temperature(t, 0.95, -40.0);
        assert!(
            app < t,
            "a grey body looks colder than it is under a cold sky"
        );
        let mut px = [app as f32];
        apply_emissivity_correction(&mut px, 0.95, -40.0).unwrap();
        assert!((px[0] as f64 - t).abs() < 1e-3);
        assert!((apparent_temperature(t, 1.0, -40.0) - t).abs() < 1e-9);
        assert!(apply_emissivity_correction(&mut px, 0.0, -40.0).is_err());
    }
}
